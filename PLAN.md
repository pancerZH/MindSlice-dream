# MindSlice-dream 设计 (PLAN.md)

> 设计确定于 2026-11-16，2026-11-17 修订（决策 8、9、10）。
> 本文件只放设计；实现记录和运行结果在本地的 REPORT.md（不进 git）。

## 1. 目标

给 `~/mindslice` 加一个离线的 dream 过程：定期把记忆卡对照真实来源（git、PyPI、CI、测试）核对和修正，并整理结构，防止记忆腐化。
思路来自 Anthropic《Components of agent memory》（2026-11-06）里的 dreaming：离线核对笔记、合并重复、修链接、保持常驻层精简。文章里拿 transcript 核对笔记的例子只有一个，其余实验都对照源文档核对；本项目也只对照能直接查到的来源，不回顾对话（决策 9）。

动机：第一次手动核对（2026-11-16）就发现，状态类卡片（进度、版本号、测试数、提交 / 发布状态）常在写下一两天内就过期。

## 2. 已确认决策（用户，2026-11-16 起）

1. 会话内写入后不跑校验，靠周期性的 dream 检查防止问题积累。
2. 会话内尽量避免任何额外的 MCP 调用。某个问题要不要在会话里提示或修复，只看两点：事后（dream 或用户 bake 时）能否以较小代价修对；留在 overlay 期间是否明显影响查询。能事后修、影响又小的，一律事后修。
   这条的前提是 agy 每次 MCP 调用要 1.5–4.0 s（Codex 只要 26–40 ms）；如果 agy 解决了高延迟，可以重新评估。
3. 按第 2 条，重名别名（`duplicate_alias`）和断链交给 dream / bake 修复。
4. 写入时的断链警告已经移除（用户同日的后续决定，随 `bake --drop` 一起实现，`e03d55f`，v0.2.1）：`write()` 不再对未解析的 `[[wikilink]]` 告警，断链只由 `validate` 报告；未解析的 extends / supersedes 仍在写入时告警，因为它们会改变读取结果。
   还剩一处与第 3 条不一致：MCP 说明仍要求在会话内处理 `affected_backlinks`（见第 12 节待定事项 2）。
5. dream 完全通过操作 MindSlice 引擎实现：dream 自己不改引擎，引擎里不引入 LLM。形态是"定时任务 + 一次 LLM agent 会话"。
6. Harness 用 agy，模型 `gemini-4-hydrogen-high`。
7. bake 仍由用户手动批准，dream 不 bake。
8. 不追求每个修改都能回溯（2026-11-17）：这做不到。在现有基础上尽量减少损失，避免过度设计带来的过度防御。所以 dream 运行前不做还原点，改错只在 overlay 里的卡造成的损失由第 7 节的写入规则来限制。
9. 不回顾会话记录做溯源（2026-11-17）：项目事实直接对照 git、PyPI、CI、测试核对。出处只用写入时已经记下的内容，即卡里写明的证据和 `mindslice history` 里的 reason。卡里写着"用户决定了 X"、而卡和 reason 都支撑不了的，不改，列进报告的"需要用户处理"。
   依据：文章用 transcript 纠错的例子只有 Mt. Moon 一个，其余实验都对照源文档；首次手动 verify pass 查出的都是状态过期、卡间矛盾和结构问题，没有把建议记成决定的；回顾对话要依赖 Claude Code 未公开的日志格式和 30 天的保留期，按 reason 匹配有一部分写入找不到对应对话，还要把对话片段交给 agy 背后的 Gemini。
   代价：会话后段做的更正没写回卡里，dream 发现不了。这类问题如果反复出现，再考虑加回对话回顾。
10. 每次运行都要清理干净（2026-11-17）：运行结束后（包括超时），会话的临时文件和它留下的进程都清掉，记忆库以外不留下文件；运行目录只保留运行记录。

## 3. 架构总览

```
systemd timer（每天 03:00）
  └─ mindslice-dream run
       1. gate（纯脚本，无 LLM）
            读 validate / diff / .history / 关注仓库 HEAD / state.json
            无事可做 → 记一条 skipped 后退出（零 LLM 成本）
       2. 准备输入（纯脚本）
            input.json：触发原因、新写入及其 reason、validate 新问题、HEAD 变化
       3. 一次 agy 会话（gemini-4-hydrogen-high）
            prompts/dream.md：新写入 → 新鲜度 → 结构 → 常驻层 → 报告
            只通过 `mindslice` CLI 读写记忆；用 git / PyPI / CI / 测试核对事实
       4. 事后检查（纯脚本）
            本次运行期间的 history 事件是否越界；validate 是否新增 error
            通过 → 推进 checkpoint；不通过 → checkpoint 不动，下次重来
```

读写边界：

- **写**：只经 `mindslice write`。history、冲突保护（`patch` 的旧文本匹配、`replace` 的 `--if-match`）、多卡改动的原子性（`--also`）、常驻卡暂存都由引擎负责。
- **读**：记忆经 `mindslice` CLI 读取。真相来源在引擎之外：git、PyPI JSON API（`https://pypi.org/pypi/<pkg>/json`）、GitHub Actions REST API、重跑测试。会话记录不读（决策 9）。

会话里统一用 CLI、不用 MCP，原因是：`validate`、`diff`、`status`、`history` 本来就只有 CLI；只用一个接口，也就不依赖 agy 的 MCP 配置。
另外 dream 在夜间运行，没人等结果，agy 工具调用慢可以接受。
能省的调用仍然省：要读多张卡时用一次 `mindslice read a b c`，它按顺序返回每张卡，读不到的在原位给出错误。

## 4. Gate（门控）

满足下面任一条件，才启动 agy 会话：

| 条件 | 数据来源 |
| :-- | :-- |
| 上次 checkpoint 之后有新的写入事件（不算 dream 自己的写入和 `invest/*`） | `.history/changes.jsonl`（只读） |
| validate 出现了上次运行没有的 error / warning | `mindslice --json validate` |
| 某个关注仓库的 HEAD 变了 | `git -C <repo> rev-parse HEAD`，仓库列表在 `config.toml` |
| 距上次全量新鲜度扫描已满 7 天 | `state.json` |
| 命令行传了 `--force` | — |

说明：

- 只比较新增的 validate 问题。有些警告（例如基础 slice 副本上的 `long_summary`）要等 bake 才会消失；如果"有警告就触发"，会每晚空跑。
- dream 自己写入时，reason 一律以 `[dream]` 开头，gate 靠这个前缀把它们排除，避免 dream 触发自己。一次 `--also` 写入会产生多条事件，每条都带各自的 reason（规则见第 7 节）。
- 关注仓库的 HEAD 是最便宜的信号：代码变了但没人写记忆时，状态卡一样会过期。
- 第一次运行（还没有 checkpoint）只把最近 `first_run_days`（默认 1）天的写入算作新写入，更早的卡由同一次的全量新鲜度扫描覆盖：积压太多时一次会话做不完，而超时的运行不推进 checkpoint，积压会越滚越大。

## 5. 输入（input.json）与出处

gate 决定运行后，脚本把本次任务写进 `input.json`：

- 触发原因，以及这次是否做全量新鲜度扫描；
- checkpoint 之后的新写入（不含 dream 自己的和 `invest/*`）：按卡的路径合并（写入后卡可能已经 bake 进某个 slice），带上每次写入的时间、op 和 reason；最后一次写入是删除的卡不列出；
- validate 新增的问题；
- HEAD 变了的关注仓库：上次和这次的 HEAD，agent 可以直接 `git log <旧>..<新>`。

出处只用写入时已经记下的内容：卡里写明的证据，以及 history 里的 reason，后者是写入的 agent 手里有完整对话时写下的。dream 不读会话记录（决策 9）；更早的 reason 由 agent 自己用 `mindslice history <地址>` 查。

## 6. dream 会话做什么

`prompts/dream.md` 主要讲清目的和目标，硬规则只保留与安全相关的（见第 7 节）。原因是文章发现，固定规则会削弱强模型的效果。
建议的处理顺序如下，便宜的在前：

1. **新写入**：逐张核对 `input.json` 里本次改动的卡和它们的 reason：卡里写成用户决定的，卡里或 reason 里有没有证据支持；卡里的事实和 reason 是否一致。支撑不了的不改，列进报告的"需要用户处理"。
2. **新鲜度**：找出易变的卡（状态、"as of"、版本号、测试数、未提交 / 未发布），对照 git、PyPI、CI、测试核对。过期就改，并加一行 `Verified YYYY-MM-DD against <source>`。
   优先处理本次新写入的卡，以及与 HEAD 变化仓库相关的卡；全量扫描到期时，覆盖全部易变卡。
   只有卡里写的测试数或测试结果正是要核对的内容时，才在那一个仓库里重跑测试，而且只用仓库现有的环境（要装依赖或新建 `.venv` 的就不跑，在报告里写明没核对）。
3. **结构**：修复 validate 报出的问题（断链、重名别名等）；rebase 尚未 rebase 的扩展卡；合并重复卡；处理卡与卡之间的矛盾；拆分涵盖多个主题的卡。
   一次调整涉及多张卡时（合并、拆分、rebase，连同随之要改的反向链接），用一次 `mindslice write ... --also <file.json>` 提交：要么全部生效，要么都不生效。会话即使被超时终止，也不会留下改了一半的状态（引擎下次打开工作区时会撤销未完成的批次）。
4. **常驻层**：常驻卡过长、或已经没必要每次加载的，提出精简或降级。引擎会自动暂存这类改动，等用户批准。
5. **报告**：写 `report.md`，内容包括：
   - 查了哪些卡、发现几张过期；
   - 做了哪些修改（地址 + 一句话理由）；
   - 需要用户处理的事项，其中包括已经没有任何卡引用的转发桩：它们是下次 bake 时 `mindslice bake @slice --drop <path>` 的候选，dream 自己不 bake；
   - 记忆规模：卡片数、常驻层字符数。

## 7. 写入策略与安全边界

prompt 中的硬规则：

- 只用 `mindslice write` 写入。每次写入都带 `--reason "[dream] <证据来源>"`；长正文用 `--content-file`。
- 改卡里的一部分（状态行、版本号、链接、个别语句）用 `--op patch --edit <旧文本> <新文本>`：只替换引用的文本，其余内容原样保留，不会因为漏写而丢语句。旧文本对不上（卡已被别的会话改过）就跳过，留到下次。
- 用 patch 改写一整行时，只改过期的部分，旧行里仍然成立的细节原样留在新行里：引用的旧文本会整段换掉，漏写的内容就丢了。
- 只有整卡重写才用 `replace`，并且一定带 `--if-match`，冲突就跳过，留到下次。
- 涉及多张卡的改动用一次 `--also` 提交（见第 6 节第 3 步）。成员默认沿用这次调用的 `--reason`；成员自带 reason 时也必须以 `[dream]` 开头，否则 gate 和事后检查会把它当成其他会话的写入。
- 不 delete，只用 patch / replace / append / supersede。
- 不执行 bake、apply、discard、mount、rollback，也不用 `--no-stage`。
- 不碰 `invest/*`，它归 news-agent 自己的审阅流程。
- 证据不明确就不改，列进报告的"需要用户处理"。
- 卡里或 reason 里没有证据支持的，不把建议升级成决定。
- 记忆库以外只读，也不在仓库或其他目录里留下文件（决策 10）；临时文件只放在会话的工作目录里。

写入模式（`config.toml` 的 `[dream] mode`，选哪个见第 12 节待定事项）：

- `direct`：直接写入 @overlay。overlay 本来就是基础 slice 的暂存区，用户 bake 时的审阅就是人工把关点。
- `stage`：所有写入都加 `--stage`，由用户用 `mindslice diff` 或 `/mindslice:review` 审阅。

两种模式都不做运行前的还原点（决策 8）。已 bake 的卡改的是 overlay 里的副本，原文仍在 slice 里；只在 overlay 里的卡在 `direct` 模式下被改错，旧文本就找不回了。这部分损失靠上面的规则限制：patch 只动引用的文本，replace 必须带 `--if-match`，不 delete。`stage` 模式下，overlay 在用户 apply 之前保持原样。

事后检查由脚本完成，检查对象是本次运行期间的 history 事件，以及运行前后的 validate 结果：

| 发现 | 处理 |
| :-- | :-- |
| dream 的 delete、`invest/*` 写入、直接生效（未暂存）的常驻卡改动、`stage` 模式下未暂存的写入；任何 bake / apply / discard / mount（`retired`）事件 | 记为 guardrail 问题；checkpoint 不推进 |
| validate 新增 error | 同上 |
| 有 reason 不以 `[dream]` 开头的写入 | 说明有其他会话同时在写，只记为并发警告；这些事件留给下次处理 |

history 事件的 op 有 create / replace / patch / append / delete 五种；脚本只按 delete 和地址判断越界，不假设 op 的集合固定。

会话超时、agy 非零退出或没有报告时，checkpoint 同样不推进。

checkpoint 记的是运行开始时的 history 位置，所以运行期间其他会话的写入会在下次运行时处理。

## 8. 运行产物与状态

```
runs/<YYYYMMDD-HHMMSS>/
  input.json        gate 结果与本次任务
  prompt.md         渲染后的 prompt
  stdout.log  stderr.log
  report.md         dream 写的报告，后面附脚本的检查结果
  meta.json         状态、耗时、触发原因、checkpoint、问题列表、resume 命令
runs/latest.md      指向最近一次的 report.md
state.json          checkpoint（最后一个 history event_id）、上次 validate 问题集合、
                    关注仓库的 HEAD、上次全量扫描日期、最近一次运行的结果
```

会话在 `runs/<id>/work/` 里工作。运行结束后（包括超时和运行器自己中断），运行目录里除上面这些运行记录以外的东西全部删除，`work/` 也一起删掉；运行器若在清理前就被杀掉，下一次运行开始时会先清理旧运行目录里的残留（决策 10）。

`runs/`、`state.json` 和本地的 `config.toml` 不进 git；仓库里放的是 `config.example.toml`。

## 9. 调度与 harness

`systemd/` 下放两个 user unit，安装时复制到 `~/.config/systemd/user/`，并把 service 里的 `@REPO@` 换成仓库所在的绝对路径（命令见 README）：

- `mindslice-dream.timer`：`OnCalendar=*-*-* 03:00:00`，`Persistent=true`。03:00 是为了避开 news-agent 的 07:30 和 18:00。
- `mindslice-dream.service`：`Type=oneshot`，`ExecStart=%h/.local/bin/uv run mindslice-dream run`，`TimeoutStartSec=90min`，`Nice=10`。

agy 命令沿用 news-agent 的配置：

```
agy --print=<prompt> --model gemini-4-hydrogen-high --output-format json \
    --dangerously-skip-permissions --disable-slash-commands
```

会话超时设为 60 分钟：agy 工具调用慢，夜间可以接受。

会话环境：

- 设 `MINDSLICE_PLUGIN_ISOLATE=1`。agy 自带的 MindSlice 插件会给每个会话注入记忆、提供 MCP 工具，用的是插件自带的引擎版本和它自己配置的工作区；隔离后它不注入、不提供工具，会话只能经 prompt 给出的 `ms`（绝对路径）读写记忆。这是插件 README 给 `agy -p` 嵌套会话的做法。
- 把 `MINDSLICE_WORKSPACE` 设为配置的工作区，漏写 `-w` 的命令也不会落到别的工作区。
- 设 `MINDSLICE_DREAM_RUN=<运行目录>` 作为这次会话所有进程的标记，再设 `PYTHONDONTWRITEBYTECODE=1`，跑测试时不在仓库里留下字节码缓存。

会话结束后（包括超时），运行器在 `/proc` 里找出环境中带这个标记的进程，先 SIGTERM，宽限期过后再 SIGKILL。按标记而不是按进程组找，是因为 agy 执行命令时可能另开进程组或会话。由 systemd 启动时，service 结束时 systemd 还会清理它 cgroup 里的进程。

## 10. 目录结构

```
MindSlice-dream/
  PLAN.md  README.md  pyproject.toml  config.example.toml（本地复制成 config.toml）
  prompts/dream.md
  src/mindslice_dream/
    cli.py          run [--force] [--dry-run]
    gate.py         触发判断与 input.json，读写 state.json
    runner.py       渲染 prompt、启动 agy、事后检查、写 meta.json / report.md
  systemd/mindslice-dream.service  mindslice-dream.timer
  tests/
```

- Python 3.12+，代码只用标准库，与 MindSlice、Forge 一致。唯一的依赖是 `mindslice>=0.2.2`（第一个带 `op=patch`、`--also` 和多地址 `read` 的版本），让 `uv run` 用上这个 CLI。
- 通过子进程调用 `mindslice` CLI，不 import 引擎内部代码。
- 代码保持短小，判断逻辑放在 prompt 里。
- `--dry-run` 只跑 gate 和输入准备，打印 `input.json`，不启动 agy。

## 11. 验证方案

### 自动化测试（`uv run pytest`）

全部使用 fake harness，不调用真实 LLM（参照 news-agent 的 `tests/fake_harness.py`）。测试在 `mindslice init` 建出的临时工作区上运行，不碰 `~/mindslice`。

- gate：空闲时跳过；四种触发条件各自生效；旧的 validate 警告不会重复触发；`[dream]` 写入和 `invest/*` 写入既不触发，也不进 `input.json`；同一张卡的多次写入在 `input.json` 里合成一条。
- 事后检查：能发现 delete、`invest/*` 写入、bake、未暂存的常驻卡改动；发现问题时 checkpoint 不推进；`patch` 事件和一次 `--also` 产生的多条事件按普通写入处理。
- 清理：运行目录只剩运行记录；会话留下的进程（包括另开会话的后台进程）在运行结束后被结束；超时时会话本身也被结束；上次中断留下的残留在下次运行开始时清掉。

### 手动端到端

1. 用 `mindslice backup` 备份 `~/mindslice`，再 `mindslice restore` 到临时目录。
2. 用一份指向副本的配置运行 `mindslice-dream --config <副本配置> run --force`（agy + gemini-4-hydrogen-high；完整命令见 README）。
3. 查看 `report.md` 和 `mindslice -w <副本> diff --scope overlay`，确认每处修改都有证据、没有越界；再确认真实工作区的 history 里没有出现 `[dream]` 写入（隔离开关生效）。
4. 确认没问题后再安装 timer，对真实工作区运行。

## 12. 待定事项

1. 写入模式：先用 `stage` 跑几次校准再切到 `direct`，还是一开始就用 `direct`。（建议：前 3 次用 `stage`；`config.toml` 默认是 `stage`）
2. `affected_backlinks` 怎么归类（建议，未定）：只是 rebase 扩展卡的，可以推迟；反向链接的卡还把旧值写成当前值的，会影响回答，可能仍应在会话内处理。
   新的选项：会话在写入前已经 `read` 过那张卡时，反向链接是已知的，修复可以用 `also` 和这次写入放进同一次调用，不再多花 MCP 调用。
3. 报告怎么送达：只保留文件，还是像 news-agent 一样发邮件。
4. 自适应频率：如果连续一周几乎查不出过期，是否拉长间隔。v1 先固定每晚运行，由 gate 跳过空闲的晚上。

## 13. 不在范围内

- dream 不修改 MindSlice 引擎（见决策 5）。
- 不做多 agent 并行：工作区在一两百张卡以内时，一个会话够用。超出时再按 slice 拆分，每个 slice 由一个会话负责。
- 不 bake，不处理 news-agent 的 `invest/*`。
- 不读会话记录（决策 9）。
