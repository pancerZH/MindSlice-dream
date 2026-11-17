# MindSlice-dream

给 MindSlice 工作区（默认 `~/mindslice`）做夜间 dream：脚本先判断今晚有没有事做，有就启动一次 agy 会话，把记忆卡对照 git、PyPI、CI 和测试核对、改正，并整理结构；会话结束后脚本检查它有没有越界。设计和决策见 [PLAN.md](PLAN.md)。

## 安装

```bash
uv sync                      # 依赖只有 mindslice>=0.2.2（op=patch、--also、多地址 read），CLI 装在 .venv 里
cp config.example.toml config.toml   # 再改 workspace、watch.repos 等；config.toml 只在本机，不进 git
uv run pytest                # fake harness，不调用真实 LLM，不碰 ~/mindslice
```

## 运行

```bash
uv run mindslice-dream run --dry-run   # 只跑 gate，打印 input.json，不启动 agy、不改 state
uv run mindslice-dream run             # gate 判断无事可做就跳过
uv run mindslice-dream run --force     # 无论 gate 结果都启动会话
uv run mindslice-dream --config other.toml run   # 换一份配置（例如指向工作区副本）
```

配置在本机的 `config.toml`（模板是 `config.example.toml`）：工作区、写入模式（默认 `stage`，所有写入暂存待审，用 `mindslice diff` 或 `/mindslice:review` 审阅）、关注的仓库、agy 命令和超时。

每次运行的产物在 `runs/<YYYYMMDD-HHMMSS>/`：`input.json`、`prompt.md`、`stdout.log` / `stderr.log`、`report.md`（dream 写的报告，后面附脚本检查）、`meta.json`（状态、触发原因、问题、resume 命令）。`runs/latest.md` 指向最近一次报告；`state.json` 存 checkpoint，只有检查通过才推进。

## 先在副本上试

```bash
uv run mindslice -w ~/mindslice backup /tmp/ms-backup            # 备份 ~/mindslice
uv run mindslice restore /tmp/ms-backup --output /tmp/ms-copy     # 恢复成一个新工作区
mkdir -p /tmp/dream-e2e && sed 's#~/mindslice#/tmp/ms-copy#' config.toml > /tmp/dream-e2e/config.toml
uv run mindslice-dream --config /tmp/dream-e2e/config.toml run --force
uv run mindslice -w /tmp/ms-copy diff                             # stage 模式：看暂存的改动
uv run mindslice -w ~/mindslice history --limit 10               # 真实工作区里不该出现 [dream] 写入
```

`state.json` 和 `runs/` 落在这份配置所在的目录（`/tmp/dream-e2e`），不会混进正式运行的状态。

## 定时

```bash
mkdir -p ~/.config/systemd/user
sed "s#@REPO@#$PWD#" systemd/mindslice-dream.service > ~/.config/systemd/user/mindslice-dream.service
cp systemd/mindslice-dream.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mindslice-dream.timer   # 每天 03:00；错过的会在开机后补跑
journalctl --user -u mindslice-dream                  # 查看输出
```
