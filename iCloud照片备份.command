#!/bin/bash
# Double-click on Mac to download a small test batch. Credentials are entered locally.
task_script_dir="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1
python3 "$task_script_dir/icloud_backup.py" --download --recent 10 --include-edits
task_exit_code=$?
printf '\n运行结束，状态码 %s。按回车关闭窗口。\n' "$task_exit_code"
read -r task_reply
exit "$task_exit_code"
