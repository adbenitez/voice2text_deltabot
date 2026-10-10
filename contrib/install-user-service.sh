#!/bin/sh
# Install the bot as a systemd --user service.
#
# Run it from the repository root, e.g.:
#
#   ./contrib/install-user-service.sh --chatmail cm1.wwire.su
#
# What it does:
#   1. uv sync                     -> project venv, incl. the matching core
#   2. create the bot account      -> DCACCOUNT:<chatmail host> or address+password
#   3. render contrib/voice2text-bot.service.in into ~/.config/systemd/user/
#   4. systemctl --user enable --now <name>.service
#   5. print the invitation link  (scan it in Delta Chat to talk to the bot)
#
# No root is needed anywhere. Use --dry-run to see what would happen.
#
# The Delta Chat core (deltachat-rpc-server) is deliberately taken from the
# project venv, not from a separate `uv tool install`: deltabot-cli requires
# deltachat2[full], which pins the core to the bindings version, and a newer core
# cannot be read by the current bindings (see the note in pyproject.toml).

set -eu

name=voice2text-bot
config_dir=
chatmail=
email=
password=
model=
compute_type=int8
extra_args=
dry_run=0
start=1
install_uv=0

usage() {
    cat <<'EOF'
Usage: install-user-service.sh [options]

  --chatmail HOST        create a chatmail account (DCACCOUNT:HOST), e.g. cm1.wwire.su
  --email ADDR           existing Delta Chat address; needs --password
  --password PASS        password for --email
  --name NAME            name of the systemd unit (default: voice2text-bot)
  --config-dir PATH      bot data/accounts directory (default: the folder the CLI
                         itself uses, ~/.config/voice2text-bot on Linux)
  --model NAME           whisper model, e.g. small|medium|large (default: bot default)
  --compute-type TYPE    int8|float16|int8_float16 (default: int8)
  --extra-args "ARGS"    extra arguments appended to "serve"
  --no-start             install the unit but do not start it
  --dry-run              print the unit and the plan, change nothing
  --install-uv           install uv automatically if it is missing
  -h, --help             this help
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --chatmail) chatmail=${2:?}; shift 2 ;;
        --email) email=${2:?}; shift 2 ;;
        --password) password=${2:?}; shift 2 ;;
        --name) name=${2:?}; shift 2 ;;
        --config-dir) config_dir=${2:?}; shift 2 ;;
        --model) model=${2:?}; shift 2 ;;
        --compute-type) compute_type=${2:?}; shift 2 ;;
        --extra-args) extra_args=${2:?}; shift 2 ;;
        --no-start) start=0; shift ;;
        --dry-run) dry_run=1; shift ;;
        --install-uv) install_uv=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

[ -f pyproject.toml ] && [ -d voice2text_deltabot ] ||
    die "run this from the repository root (pyproject.toml not found)"

repo=$(pwd)

# Default to the config folder the CLI itself uses (appdirs), so an account created
# by a plain `voice2text-bot init` is found instead of a second one being created
# next to it.
if [ -z "$config_dir" ]; then
    config_dir=$(uv run --no-sync python -c \
        'from appdirs import user_config_dir; print(user_config_dir("voice2text-bot"))' \
        2>/dev/null || true)
    [ -n "$config_dir" ] || config_dir=${XDG_CONFIG_HOME:-$HOME/.config}/voice2text-bot
fi

# A silently created second account would break an existing chat with the bot, so
# report where accounts already live when this folder has none.
other_account_dirs() {
    for dir in "$HOME/.config/voice2text-bot" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/voice2text-bot"; do
        if [ "$dir" != "$config_dir" ] && [ -d "$dir/accounts" ]; then
            printf '%s\n' "$dir"
        fi
    done
}

unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
unit_path=$unit_dir/$name.service

if ! command -v uv >/dev/null 2>&1; then
    if [ "$install_uv" = 1 ]; then
        command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
        printf 'Installing uv...\n'
        curl -LsSf https://astral.sh/uv/install.sh | sh
        PATH=$HOME/.local/bin:$PATH
        export PATH
        command -v uv >/dev/null 2>&1 || die "uv still not on PATH; open a new shell and retry"
    else
        die "uv is not installed. Install it (https://docs.astral.sh/uv/) or re-run with --install-uv"
    fi
fi

bot=$repo/.venv/bin/voice2text-bot
exec_args="--config-dir $config_dir --compute-type $compute_type"
[ -n "$model" ] && exec_args="$exec_args --model $model"
[ -n "$extra_args" ] && exec_args="$exec_args $extra_args"
exec_args="$exec_args serve"

# run the CLI from the project environment (uv puts .venv/bin on PATH)
run() {
    uv run --no-sync voice2text-bot "$@"
}

render_unit() {
    sed -e "s|@REPO@|$repo|g" \
        -e "s|@EXEC@|$bot $exec_args|g" \
        contrib/voice2text-bot.service.in
}

if [ "$dry_run" = 1 ]; then
    printf 'plan:\n'
    printf '  repo        %s\n' "$repo"
    printf '  config dir  %s\n' "$config_dir"
    printf '  unit        %s\n' "$unit_path"
    printf '  commands    uv sync\n'
    if [ -n "$chatmail" ]; then
        printf '              %s --config-dir %s init DCACCOUNT:%s\n' "$bot" "$config_dir" "$chatmail"
    elif [ -n "$email" ]; then
        printf '              %s --config-dir %s init %s <password>\n' "$bot" "$config_dir" "$email"
    else
        printf '              (account is left as is; pass --chatmail or --email to create one)\n'
    fi
    printf '              systemctl --user daemon-reload && systemctl --user enable --now %s\n' "$name"
    printf '\n%s\n' "$unit_path:"
    render_unit
    exit 0
fi

printf '1/5 syncing the project environment with uv...\n'
uv sync

printf '2/5 checking for an account in %s...\n' "$config_dir"
mkdir -p "$config_dir"
accounts=$(run --config-dir "$config_dir" list 2>/dev/null || true)
if [ -n "$accounts" ]; then
    printf '    found:\n%s\n' "$accounts"
else
    others=$(other_account_dirs)
    if [ -n "$others" ]; then
        printf '    no account in %s, but accounts exist in:\n' "$config_dir"
        printf '      %s\n' $others
    fi
    if [ -n "$chatmail" ]; then
        printf '    creating a chatmail account on %s...\n' "$chatmail"
        run --config-dir "$config_dir" init "DCACCOUNT:$chatmail"
    elif [ -n "$email" ] && [ -n "$password" ]; then
        printf '    configuring %s...\n' "$email"
        run --config-dir "$config_dir" init "$email" "$password"
    elif [ -n "$others" ]; then
        die "refusing to create a second account: re-run with --config-dir <one of the paths above>, or pass --chatmail HOST"
    else
        die "no account in $config_dir: pass --chatmail HOST, or --email ADDR --password PASS"
    fi
fi

printf '3/5 invitation link (scan it in Delta Chat):\n'
run --config-dir "$config_dir" link | sed -n 's|^\(https://i.delta.chat/#.*\)$|    \1|p'

printf '4/5 installing the unit %s...\n' "$unit_path"
mkdir -p "$unit_dir"
render_unit >"$unit_path"
systemctl --user daemon-reload

if [ "$start" = 1 ]; then
    printf '5/5 starting %s.service...\n' "$name"
    systemctl --user enable --now "$name.service"
    printf '\nstatus:  systemctl --user status %s.service\n' "$name"
    printf 'logs:    journalctl --user -u %s.service -f\n' "$name"
    printf 'stop:    systemctl --user disable --now %s.service\n' "$name"
    printf '\nFirst start downloads the whisper model (large ~2.9 GB) and needs a few\n'
    printf 'minutes before the bot answers; watch the logs.\n'
    printf 'To keep it running without an active session: loginctl enable-linger %s\n' "$(id -un)"
else
    printf '5/5 --no-start given; start it with:\n'
    printf '    systemctl --user enable --now %s.service\n' "$name"
fi
