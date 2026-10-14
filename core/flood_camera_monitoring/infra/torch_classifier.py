from __future__ import annotations

"""Adapter: classificador de alagamentos baseado em PyTorch.

Implementa a porta de domínio FloodClassifierPort para integrar o modelo
treinado (checkpoint .pth) com a aplicação.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Union
import os
import logging

import io
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as T
import torchvision.models as tv_models
from PIL import Image

from core.flood_camera_monitoring.services.stream_prediction import (
    FloodAssessment,
    FloodProbabilities,
    ImageInput,
    FloodSeverity,
)


def _to_pil(image: ImageInput) -> Image.Image:
    if isinstance(image, (str, Path)):
        return Image.open(image).convert("RGB")
    if isinstance(image, (bytes, bytearray)):
        return Image.open(io.BytesIO(image)).convert("RGB")
    raise TypeError(f"Unsupported image input type: {type(image)}")


@dataclass
class TorchFloodClassifier:
    checkpoint_path: Union[str, Path]
    device: Union[str, torch.device] = "cpu"

    @staticmethod
    def _build_builtin_model(model_name: str, num_classes: int) -> torch.nn.Module:
        ctor = getattr(tv_models, model_name, None)
        if ctor is None:
            raise ValueError(f"Unsupported backbone '{model_name}'")

        try:
            backbone = ctor(weights=None)
        except TypeError:
            backbone = ctor(pretrained=False)

        in_features = int(backbone.fc.in_features)
        backbone.fc = nn.Sequential(
            nn.Dropout(p=0.3),
            nn.Linear(in_features, 512),
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.2),
            nn.Linear(512, int(num_classes)),
        )

        class _Wrapper(nn.Module):
            def __init__(self, wrapped):
                super().__init__()
                self.backbone = wrapped

            def forward(self, x):
                return self.backbone(x)

        return _Wrapper(backbone)

    def __post_init__(self) -> None:
        logger = logging.getLogger(__name__)
        self.device = torch.device(self.device)
        self._fallback = False
        ckpt: dict | None = None
        model_from_script: torch.nn.Module | None = None

        path = Path(str(self.checkpoint_path))
        if not path.exists() or path.is_dir():
            logger.warning(
                "Checkpoint ausente ou inválido em '%s' – usando modelo fallback simples.",
                path,
            )
            self._init_fallback_model()
            return

        # Detectar pointer Git LFS (começa com 'version https://git-lfs.github.com')
        try:
            with path.open("rb") as fh:
                head = fh.read(256)
            if head.startswith(b"version https://git-lfs.github.com"):
                logger.error(
                    "Arquivo '%s' parece ser um pointer Git LFS (não baixado). Execute 'git lfs pull'. Ativando fallback.",
                    path,
                )
                self._init_fallback_model()
                return
        except Exception as e:  # pragma: no cover
            logger.warning(
                "Não foi possível inspecionar o arquivo do modelo (%s). Fallback.", e
            )
            self._init_fallback_model()
            return

        # Tentativas de carregamento
        load_errors: list[str] = []

        def _wrap_load(fn_desc: str, loader_fn):  # helper interno
            try:
                return loader_fn()
            except Exception as e:  # pragma: no cover - resiliente
                load_errors.append(f"{fn_desc}: {e}")
                return None

        # 1. torch.load (state_dict ou objeto)
        maybe = _wrap_load(
            "torch.load(simple)",
            lambda: torch.load(str(path), map_location=self.device),
        )
        if maybe is not None:
            if isinstance(maybe, dict) and "model_state_dict" in maybe:
                ckpt = maybe
            else:
                ckpt = {
                    "model_state_dict": maybe,
                    "config": {"model_name": "resnet50", "num_classes": 2},
                    "class_names": ["normal", "flooded"],
                }
        else:
            # 2. torch.load (weights_only flag)
            maybe2 = _wrap_load(
                "torch.load(weights_only=False)",
                lambda: torch.load(
                    str(path), map_location=self.device, weights_only=False
                ),
            )
            if maybe2 is not None:
                if isinstance(maybe2, dict) and "model_state_dict" in maybe2:
                    ckpt = maybe2
                else:
                    ckpt = {
                        "model_state_dict": maybe2,
                        "config": {"model_name": "resnet50", "num_classes": 2},
                        "class_names": ["normal", "flooded"],
                    }
            else:
                # 3. TorchScript
                model_from_script = _wrap_load(
                    "torch.jit.load",
                    lambda: torch.jit.load(str(path), map_location=self.device),
                )

        if model_from_script is not None:
            self.class_names = ["normal", "flooded"]
            self.model = model_from_script
            self.model.to(self.device)
            self.model.eval()
            logger.info("Modelo TorchScript carregado com sucesso (%s)", path.name)
        elif ckpt is not None:
            config = ckpt.get("config", {"model_name": "resnet50", "num_classes": 2})
            self.class_names = ckpt.get("class_names", ["normal", "flooded"])
            self.checkpoint_config = dict(config)
            model_name = str(config.get("model_name", "resnet50"))
            num_classes = len(self.class_names)
            try:
                from core.flood_camera_monitoring.infra.machine_model.model import get_model  # type: ignore

                self.model = get_model(
                    model_name,
                    num_classes=num_classes,
                    pretrained=False,
                )
            except Exception as e:
                logger.warning(
                    "Falha ao importar get_model (%s). Usando construtor embutido.", e
                )
                self.model = self._build_builtin_model(model_name, num_classes)

            try:
                self.model.load_state_dict(ckpt["model_state_dict"])  # type: ignore[index]
            except RuntimeError as e:
                logger.warning(
                    "load_state_dict estrito falhou (%s). Tentando strict=False.", e
                )
                self.model.load_state_dict(ckpt["model_state_dict"], strict=False)  # type: ignore[index]

            self.model.to(self.device)
            self.model.eval()
            logger.info(
                "Checkpoint carregado (%s) com classes %s",
                path.name,
                self.class_names,
            )
        else:
            logger.error(
                "Falha ao carregar '%s'. Erros: %s. Ativando fallback.",
                path,
                "; ".join(load_errors) or "desconhecido",
            )
            self._init_fallback_model()
            return

        self.transform = T.Compose(
            [
                T.Resize((224, 224)),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )

    def _init_fallback_model(self):
        """Inicializa um modelo mínimo para manter a API funcional.

        Produz probabilidades estáticas (normal=90%, flooded=10%) para evidenciar
        que é um modo de contingência.
        """
        logger = logging.getLogger(__name__)
        self._fallback = True
        self.class_names = ["normal", "flooded"]
        self.model = nn.Sequential(nn.Flatten(), nn.Linear(224 * 224 * 3, 2))
        # Inicializa pesos com valores pequenos para evitar NaNs.
        for p in self.model.parameters():  # pragma: no cover (simples)
            torch.nn.init.constant_(p, 0.0)
        self.model.to(self.device)
        self.model.eval()
        self.transform = T.Compose(
            [
                T.Resize((224, 224)),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )
        logger.warning("Usando modelo fallback simples (checkpoint ausente/corrompido)")

    @torch.inference_mode()
    def predict(self, image: ImageInput) -> FloodAssessment:
        pil = _to_pil(image)
        x = self.transform(pil).unsqueeze(0).to(self.device)
        logits = self.model(x)
        if self._fallback:
            # Força distribuição estável indicando modo de contingência
            # (90% normal, 10% flooded / medium inexistente)
            logits = torch.tensor(
                [[2.1972246, 0.0]], device=self.device
            )  # log(9)=2.1972 aprox
        probs = F.softmax(logits, dim=1)[0].detach().cpu().numpy()

        names = [
            str(c).lower() for c in getattr(self, "class_names", ["normal", "flooded"])
        ]
        # Mapear índices conhecidos
        try:
            i_norm = names.index("normal")
        except ValueError:
            i_norm = 0
        try:
            i_flood = names.index("flooded")
        except ValueError:
            i_flood = 1 if len(probs) > 1 else 0
        i_med = None
        if "medium" in names:
            try:
                i_med = names.index("medium")
            except ValueError:
                i_med = None

        # Extrai probabilidades brutas (softmax)
        p_normal = float(probs[i_norm]) if i_norm < len(probs) else 0.0
        p_flooded = float(probs[i_flood]) if i_flood < len(probs) else 0.0
        p_medium = (
            float(probs[i_med]) if (i_med is not None and i_med < len(probs)) else 0.0
        )

        # Converte para porcentagens e normaliza para somar exatamente 100
        pcts = [p_normal * 100.0, p_flooded * 100.0]
        if i_med is not None:
            pcts.append(p_medium * 100.0)
        total = sum(pcts)
        if total <= 0:
            # fallback seguro
            if i_med is None:
                pct_normal, pct_flooded = 50.0, 50.0
                pct_medium = 0.0
            else:
                pct_normal, pct_flooded, pct_medium = 33.34, 33.33, 33.33
        else:
            scale = 100.0 / total
            pcts = [v * scale for v in pcts]
            if i_med is None:
                pct_normal, pct_flooded = pcts
                pct_medium = 0.0
            else:
                pct_normal, pct_flooded, pct_medium = pcts

        # Classe predita e confiança coerente com a classe escolhida
        # Considera todas as classes disponíveis
        if i_med is None:
            values = [pct_normal, pct_flooded]
            idx = int(values.index(max(values)))
            pred_label = ["normal", "flooded"][idx]
        else:
            values = [pct_normal, pct_flooded, pct_medium]
            labels = ["normal", "flooded", "medium"]
            idx = int(values.index(max(values)))
            pred_label = labels[idx]

        is_flooded = pred_label == "flooded"
        confidence = float(values[idx])

        probabilities = FloodProbabilities(
            normal=pct_normal, flooded=pct_flooded, medium=pct_medium
        )
        # Define severidade coerente com o rótulo predito
        if pred_label == "flooded":
            severity = FloodSeverity.FLOODED
        elif pred_label == "medium":
            severity = FloodSeverity.MEDIUM
        else:
            severity = FloodSeverity.NORMAL
        return FloodAssessment(
            meta={},
            confidence=confidence,
            is_flooded=is_flooded,
            severity=severity,
            probabilities=probabilities,
        )
