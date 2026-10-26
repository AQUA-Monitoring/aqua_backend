
#!/bin/bash

# Diretório padrão
diretorio="${1:-aqua_backend}"

# Número mínimo de linhas (0 = mostrar todos)
limite="${2:-0}"

# Validações
if [[ ! -d "$diretorio" ]]; then
    echo "Erro: diretório '$diretorio' não encontrado."
    exit 1
fi

if ! [[ "$limite" =~ ^[0-9]+$ ]]; then
    echo "Erro: o limite deve ser um número inteiro."
    exit 1
fi

echo "Analisando diretório: $diretorio"
echo "Filtro: arquivos com mais de $limite linhas"
echo "----------------------------------------"

total_arquivos=0
total_linhas=0

# Percorre os arquivos recursivamente,
# ignorando dependências e arquivos gerados
while IFS= read -r -d '' arquivo; do

    # Considera somente arquivos relevantes ao projeto
    case "${arquivo##*/}" in
        *.py|*.js|*.jsx|*.ts|*.tsx|\
        *.html|*.css|*.scss|*.sql|*.sh|\
        *.yaml|*.yml|*.toml|*.ini|*.cfg|\
        *.vue|*.java|*.go|*.rs|\
        Dockerfile|Containerfile|\
        requirements*.txt|package.json|\
        tsconfig*.json)
            ;;
        *)
            continue
            ;;
    esac

    # Ignora arquivos minificados
    case "$arquivo" in
        *.min.js|*.min.css)
            continue
            ;;
    esac

    # Conta as linhas do arquivo
    if ! linhas=$(awk 'END {print NR}' "$arquivo" 2>/dev/null); then
        continue
    fi

    # Aplica o filtro (0 = todos)
    if (( limite == 0 || linhas > limite )); then
        printf "%6d linhas | %s\n" "$linhas" "$arquivo"

        ((total_arquivos++))
        ((total_linhas += linhas))
    fi

done < <(
    find "$diretorio" \
        -type d \( \
            -name ".git" -o \
            -name ".venv" -o \
            -name "venv" -o \
            -name "env" -o \
            -name "node_modules" -o \
            -name "__pycache__" -o \
            -name ".pytest_cache" -o \
            -name ".mypy_cache" -o \
            -name ".ruff_cache" -o \
            -name ".cache" -o \
            -name ".tox" -o \
            -name ".idea" -o \
            -name ".vscode" -o \
            -name "dist" -o \
            -name "build" -o \
            -name "vendor" -o \
            -name "site-packages" -o \
            -name "migrations" -o \
            -name "staticfiles" -o \
            -name "media" -o \
            -name "coverage" -o \
            -name "htmlcov" -o \
            -name ".next" -o \
            -name ".nuxt" -o \
            -name ".turbo" -o \
            -name "generated" \
        \) -prune -o \
        -type f -print0
)

echo "----------------------------------------"
echo "Total de arquivos: $total_arquivos"
echo "Total de linhas:   $total_linhas"
