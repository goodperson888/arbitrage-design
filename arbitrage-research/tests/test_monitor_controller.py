from pathlib import Path
import subprocess


def test_stop_pid_selection_excludes_screen_and_login_wrappers():
    script = Path(__file__).parents[1] / "scripts/monitor/arb-monitor"
    source = script.read_text()
    function = "process_pids() {" + source.split("process_pids() {", 1)[1].split("\n}", 1)[0] + "\n}"
    program = r'''
monitor_script() { printf '%s' /project/monitor.py; }
pgrep() { printf '%s\n' 101 102 103 104 105 106; }
ps() {
  case "$2" in
    101) printf 'SCREEN     \n' ;;
    102) printf 'login      \n' ;;
    103) printf 'python3.12 \n' ;;
    104) printf 'node       \n' ;;
    105) printf 'zsh        \n' ;;
    106) return 1 ;;
  esac
}
'''
    result = subprocess.run(["bash", "-c", program + function + "\nprocess_pids paper"],
                            text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ["103", "104"]
