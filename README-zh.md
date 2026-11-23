# MindSlice-dream

[English](README.md)

给 MindSlice 工作区（默认 `~/mindslice`）做夜间 dream：脚本先跑 `mindslice gc`（删掉旧版本 slice 和被杀的写入留下的残留），再判断今晚有没有事做，有就启动一次 agy 会话，把记忆卡对照 git、PyPI、CI 和测试核对、改正，并整理结构；会话拿到的 `mindslice` 由程序把关（stage 模式下自动暂存每次写入，拒绝删卡、apply 等），会话结束后脚本再检查它有没有越界，再把暂存的改动发邮件给你审阅，你回复邮件就能批准、放弃或修改。设计和决策见 [PLAN.md](PLAN.md)。

## 安装

```bash
uv sync                      # 依赖只有 mindslice>=0.2.9 和 markdown-it-py>=3.0，CLI 装在 .venv 里
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

配置在本机的 `config.toml`（模板是 `config.example.toml`）：工作区、写入模式（默认 `stage`，所有写入暂存待审，用邮件、`mindslice diff` 或 `/mindslice:review` 审阅）、关注的仓库、审阅邮件的收件地址、agy 命令和超时。

每次运行的产物在 `runs/<YYYYMMDD-HHMMSS>/`：`input.json`、`prompt.md`、`stdout.log` / `stderr.log`、`report.md`（dream 写的报告，后面附整卡删除建议和脚本检查）、`drops.json`（有删卡建议时）、`meta.json`（状态、触发原因、问题、resume 命令）。`runs/latest.md` 指向最近一次报告；`state.json` 存 checkpoint，只有检查通过才推进。

## 先在副本上试

```bash
uv run mindslice -w ~/mindslice backup /tmp/ms-backup            # 备份 ~/mindslice
uv run mindslice restore /tmp/ms-backup --output /tmp/ms-copy     # 恢复成一个新工作区
mkdir -p /tmp/dream-e2e && sed 's#~/mindslice#/tmp/ms-copy#' config.toml > /tmp/dream-e2e/config.toml
uv run mindslice-dream --config /tmp/dream-e2e/config.toml run --force
uv run mindslice -w /tmp/ms-copy diff                             # stage 模式：看暂存的改动
uv run mindslice -w ~/mindslice history --limit 10               # 真实工作区里不该出现 [dream] 写入
```

`state.json` 和 `runs/` 落在这份配置所在的目录（`/tmp/dream-e2e`），不会混进正式运行的状态。这份配置沿用了 `[mail] to`，副本上的改动也会发审阅邮件；不想收就把副本配置里的 `to` 改成 `""`。

## 邮件审阅

`config.toml` 的 `[mail] to` 填上自己的邮箱（例如发件账号的 plus 地址 `<账号>+dream@gmail.com`）就会开启；发信和收信用 `account` 指向的 Gmail 账号文件（news-agent 的 `smtp.toml`，只用其中的 host、port、username、password）。

- 夜间运行结束后，如果 staging 里有 dream 的改动，或者运行没有成功，就发一封邮件：每处改动一项，有编号、卡片和 reason，后面附运行报告。逐卡的 diff 默认不发，`[mail] attach_diff = true` 时作为附件发出。待审的改动和上一封完全一样时不重发。
- 直接回复邮件，写明编号和处理方式，例如"全部批准""批准 1-5，放弃 6""3：把 X 改成 Y 再批准"。没提到的保持待审。
- 每 10 分钟的 timer 取回复：一个 agy 会话把回复解读成每个编号的决定，程序执行（修改是先 patch 暂存版本再 apply），然后在同一个线程里回一封确认邮件，修改过的卡附最终文本。
- 只认这样的回复：引用了那封审阅邮件、从这个账号发出（Gmail 的"已发"标签）、发件人和收件人都是 `to` 的邮箱。发信后又变过的改动不执行。

```bash
uv run mindslice-dream mail          # 取一次回复并执行（timer 跑的就是它；没有待审的就直接退出）
uv run mindslice-dream mail --send   # 立刻把 staging 里 dream 的改动发一封审阅邮件
```

每处理一封回复会留下 `runs/<时间>-review/`：`input.json`（回复正文和改动）、`prompt.md`、两个日志、`report.md`（确认邮件的正文）、`meta.json`（决定和执行结果）。

## 整卡删除建议

dream 不删卡。可以整张删掉的卡（转发桩、做完了或内容都已挪走的卡）写进 `runs/<id>/drops.json`，每张带理由、可核对的依据和删前要挪走的语句。程序检查后把没处理的候选存在 `state.json` 里跨运行保留，并列进报告（所以也在审阅邮件里，不编号，回复邮件不处理它们），附上卡所在的 slice、@overlay 里的副本，以及还链接它、扩展它的生效卡片。执行（挪内容、改链接、`mindslice bake @slice --drop <path>`）在 Claude 会话里做。

```bash
uv run mindslice-dream drops                               # 列出没处理的候选和当前的链接；卡删掉后自动移出
uv run mindslice-dream drops --keep <card> --why "<原因>"   # 决定保留：dream 以后不再提
```

## 定时

```bash
mkdir -p ~/.config/systemd/user
sed "s#@REPO@#$PWD#" systemd/mindslice-dream.service > ~/.config/systemd/user/mindslice-dream.service
cp systemd/mindslice-dream.timer ~/.config/systemd/user/
sed "s#@REPO@#$PWD#" systemd/mindslice-dream-mail.service > ~/.config/systemd/user/mindslice-dream-mail.service
cp systemd/mindslice-dream-mail.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mindslice-dream.timer        # 每天 03:00；错过的会在开机后补跑
systemctl --user enable --now mindslice-dream-mail.timer   # 每 10 分钟取一次审阅回复
journalctl --user -u mindslice-dream -u mindslice-dream-mail   # 查看输出
```
