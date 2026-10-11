# v4.17.0 验证记录 / Validation Record

## 2026-10-12 回查与恢复修复：局部实测仍未验收

**325 项离线测试通过。** 新审核 `linear_source_grounding_v5` 按完整断言、数字和经过校验的叶证据定位原文，预留完整父片段上下文及 1K 错误反馈预算。原文不足时使用待补证状态，最多两轮真实新增原文的回查，并共享原三次协议调用上限。回查不改变稿件，不代替最终审核。

独立只读复查发现并修复了三个边界问题：同输入/产物的存储失败标记须跨所有剩余父尝试保留；转义量大的可选邻句不能变成必需证据；JSON 字段顺序变化不能算新增原文。新增回归验证上述保护及旧异常类型兼容。完整原文/稿件不截断，有效否定回执不会重抽，缺少回执的历史投票会阻止重评。

真实 Agnes 局部测试只使用用户已授权的访谈隔离稿，历史三集作业、转录和个人笔记未修改：

| 局部案例 | 真实结果 | 结论 |
| --- | --- | --- |
| 平台与未来项目 | 两次合格的补证请求；第三次缺少必填字段，保留三次上限 | 没有最终审核结论，不重跑相同失败请求 |
| 演员 | 一次补证请求，后两次缺少必填字段 | 没有最终审核结论 |
| 配乐 | 首次批评的文字不在所供稿件；后两次请求补证 | 到达预算上限，仍待原文确认，不标成事实错误或通过 |

三组共 9 次真实调用，5 次返回有效补证请求；这些不是通过票。已完成补证流程的局部行为验证，但没有合格的最终局部结论，也没有整集报告验收。配乐与演员使用最终检索规划；平台测试在边界复查前启动，不能拿它证明后续边界修复已实测。漏字段错误现在明确列出缺失项，不由脚本猜补模型结论；部分后加诊断只做离线回归。

新的完整访谈规划有 67 个审核任务，最大提示 21999 UTF-8 字节。本轮只执行局部诊断，未运行全部 67 项，因此不把少量局部调用宣称为完整质量或成本验证。更小审核分片增加了调用数量，尚不声称比旧规划更省总 token/耗时。

没有调用 Gemini/Groq，没有修改默认模型或扩大真实材料外发范围；仍未做正式课程与 256K 容量实测。代码可作为用户要求的本地试用更新；GitHub main 不应据此合并为验收通过的正式版。

English: 325 offline checks pass. v5 recovery uses validated provenance, claim/number anchors and bounded original context. Two lookups share the three-call limit, and durable judgments survive storage failures without new voting. A read-only review found three edge bugs, now covered by regressions. Nine targeted Agnes calls yielded five valid source requests but no final accepted verdict; original jobs remain unchanged. The full 67-job audit, formal course and 256K capacity were not tested. This is trial code, not release acceptance or a cost-efficiency claim.

## 2026-10-11 本轮结束检查点：未通过发布验收

**288 项离线测试通过；本轮模型任务均已结束，私有调用配置已清理。没有最终合格报告，没有合并 main，没有更新 OpenClaw 正式安装。** 预算修复经独立代码复查；后续遗漏记录诊断补丁通过离线回归，但新提示尚未实测。

| 样本 | 当前可复用成果 | 阻塞与未完成内容 |
| --- | --- | --- |
| 《灵动岛》 | 完整提取及正文、知识、覆盖草稿；5 条引述机械匹配 | 写作三次 blocked；审核输出非 JSON，另有有效否定意见；整稿未通过 |
| 《平原上的摩西》 | 16 个提取块、13 个归纳任务、全部正文、两个 scoped 覆盖检查；11 条引述机械匹配 | 43 个原文审核任务中保存 36 个结论（15 passed、21 failed）；下一任务三次误用遗漏类型，写作三次 blocked；不是完整审核 |
| 《纵横四海 EP86》 | 88/88 提取任务、6303/6303 原始片段，第一层归纳首组通过 | 第二组三次后 blocked，最后为第 13 行未闭合 JSON 字符串；其余 12 组、下游写作和验收尚未执行 |

长样本失败提取块 `extract-0081` 显式拆成三个完整输入子任务，三者首次通过；旧三次记录及先前合格任务保留。当前代际为 `05ef4d07bf7a73060fc9`，两次提取拆分增加的任务数不冒充原始片段覆盖率。归纳失败记录没有清零或猜补完成标记。

审稿的 passed/failed 是模型判断，不是人工确认的准确率。21 个否定尚未逐项确认，已观察到把其他原文批次的话题误判为遗漏的情况，不能将其当作 21 个真实事实错误。

新增诊断修复：无遗漏时明确要求不输出遗漏记录；空、未知、非字符串证据编号及空原因均继续拒绝，错误指出记录位置和实际编号。原输出不被修改。当前三个样本均到达保留的重试上限，不能重复发起相同失败任务以获取通过结论。

128K 私有配置用于真实 Agnes 调用，但低输入请求不证明窗口极限。256K 和正式课程未实测；已扫描用户提供的课程目录，未识别正式课程样本，不能将学习播客冒充课程。最终发布还需要整集质量验收，不能用离线通过、模型退出成功、已生成草稿或引述机械通过代替。

English: 288 offline tests pass. All current live workers have stopped and private invocation configs were removed. The short writer remains blocked; the interview saved 36/43 reviews (15 positive, 21 negative) before another review exhausted its protocol budget. The long sample completed all 88 extraction tasks and 6303 original segments plus one reduction; the next reduction exhausted three calls with incomplete JSON. No accepted final report, main merge or stable installation update is claimed. New omission diagnostics are offline-tested only. Formal-course and 256K acceptance remain untested.

## 2026-10-11 预算复查与继续验证（历史检查点）

最新代码 **287 项离线测试通过**，Skill 格式、Python 编译及 Git 空白检查通过；独立只读代码复查没有剩余实质问题。新增修复：原文覆盖批次先预留相邻原句，再填充可选正文；完整 grounding 请求的 JSON 序列化开销也在稿件入队时检查，可分内容按完整句拆分，真正不可分内容仍拒绝。

访谈草稿本地重新规划：27 个覆盖批次均保留上下文，1101 个原句恰好覆盖一次，最大完整提示为 22999 UTF-8 字节；旧规划 26 批次中 22 个没有上下文。这是本地规划检查，不是新审核规划的真实模型通过。正在执行的访谈审核仍使用进程先前载入的旧规划，不能据其结果宣称新修复已实测。

本轮草稿引述独立机械核对：访谈 11/11、短样本 5/5 与逐字原文及四舍五入的父片段起始时间匹配；不证明说话人身份、总结语义或主题完整性。长学习样本已通过 83/86 个提取任务，覆盖 6122/6303 个原始片段；后续模型调用仍仅使用授权 Agnes，未重新转录。

English: 287 offline tests pass, with independent read-only code review. Source context is reserved before optional body retrieval, and full serialized grounding requests are checked before admitting divisible artifacts. Local replanning covers all 1101 interview source units in 27 context-bearing batches, within 22999 bytes. This is not live acceptance of the new planner; the already-running review retains its previously loaded implementation. Exact-text/parent-time checks pass for 11 interview and five short-draft quotations. Live episode acceptance, formal-course and 256K tests remain incomplete; main and the installed Skill stay unchanged.

## 2026-10-11 Agnes 专项修复与续测（历史检查点）

最新检查点：**284 项离线测试通过。** 访谈旧写作任务因将约 75K 字节正文重复放进 23K 上限的覆盖请求，三次后 blocked。新增完整段落检索式覆盖与一次性显式协议迁移；旧三次记录、全部合格证据/正文缓存、每部分次数及相同提示的否定审稿意见保留，不重启无关语义失败。两项新的 Agnes 覆盖请求已经实际通过，处理全部 40 个证据编号，输入为 22895 / 17447 UTF-8 字节，全文未删短。后续独立原文审稿正在运行，已有通过和否定意见，尚未整集验收。

长学习播客现有 65/86 个提取任务通过，覆盖 5109/6303 个原始片段，继续仅使用 Agnes。《灵动岛》仍保留三次失败；最后一个审稿协议返回非 JSON 的英文推理文本，未猜测或截取部分响应伪称通过。用户提供的课程目录已扫描 593 份元数据，尚未识别正式课程样本。正式安装及 main 未变，256K 未实测。

English: 284 offline checks pass. The long interview's full body remains intact while two real scoped coverage calls pass for all 40 evidence nodes, using 22895 and 17447 UTF-8 bytes. Full-source reviewing continues with both positive and negative judgments. The long learning sample has 65/86 extraction tasks and 5109/6303 original segments covered. The short audit remains blocked after its protocol limit. No final release, formal-course or 256K acceptance is claimed.

续测检查点：**266 项离线测试通过。** 《灵动岛》十个正文片段、知识与覆盖文件已生成；首个原文覆盖审稿返回有效的否定结论，涉及原文没有的年份及术语异文，不是内容通过。最终报告尚未通过。《平原上的摩西》第二层六个归纳任务均通过，进入逐片段正文写作。不能把文件生成或协议返回成功当作语义验收通过。

新增独立审稿协议重试及持久记录：每个确切源/任务/模型/配置/提示键最多三次。格式、地址或结构错误可以重试；有效否定结论保留且不重抽。旧直接审稿调用计数在首次迁移时导入，避免升级清空预算。新的审核补丁有 5 项离线回归，历史输入的行为保持兼容。

English: 266 offline tests pass. The short body, knowledge and coverage are written, but its first source review returned a valid rejection, not a quality pass. All six second-level interview reductions passed. Independent review protocol retries preserve previous dispatch counts; valid negative judgments are not rerun unchanged.

**261 项离线测试通过；仅使用 Agnes，真实整集验收仍未通过。** 用户明确选择继续仅使用 `agnes-ai/agnes-3.0-flash`，没有将测试转录、证据或草稿发给 MiniMax。以下旧检查点仅是历史，不代表现在停止续测。

- 失败提取块/归纳组新增显式 `refine`：完整片段、完整主题及原始证据不删减；新子任务输入更小，旧任务三次失败、所有合格旁支及完整状态快照保留。原文或依赖变化、正在运行的任务及不可分输入均拒绝拆分。离线覆盖包括真实准备图恢复、跨层来源与整条主题拆分，但模拟不证明总结语义质量。
- 正文片段改用 Markdown，知识、覆盖与独立审稿保留结构化输出。每个片段的三次调用次数跨父任务启动持久保存，缓存按模型/配置/提示/输出绑定；父写作任务仍只有三次启动。
- 《灵动岛》Markdown 写作测试 `21dfa9373744cf28e713` 的三个片段通过，但详细正文片段三次仍引入源中没有的英文人名而 blocked。未伪称完整验收通过。新增中文优先和原样姓名明确提示及章节级检查，建立新提示代际 `8712aaccad8d3e4357fd`；旧记录保留。
- 《平原上的摩西》16 个提取块与原先两个合格归纳组复用；失败归纳组拆分为四个新任务，全部真实通过，随后剩余归纳组也通过。下一层三次超过 6K 输出预算，已在完整主题边界重新拆为五个任务，尚待模型验证。所有原始证据成员继续绑定。
- 《纵横四海 EP86》旧失败提取块拆为三个子任务，全部真实通过，其中一个经历一次严格 JSON 解析失败后第二次通过。31 个既有合格块保留，另一个后续块通过，当前 35/86 个提取任务完成，对应 2610/6303 个完整原始片段。后续一块首次解析失败，尚余两次，停在可恢复检查点；不能将拆分后的任务计数当作 35/84 原始块覆盖率。
- 128K 调用配置继续用于实际 Agnes 会话；输入预算、模型标称容量和真实峰值上下文分开报告。尚无 256K 容量实测，正式课程样本仍未在用户给定目录中识别。没有更新 OpenClaw 正式安装或合并 GitHub main。

English: 261 offline checks pass. Only the authorized Agnes model is used. Smaller whole-input refinement preserves failed parent ledgers and validated siblings. Three short sections and all four refined interview reductions passed, but the short writer and a later interview reduction still failed their three-attempt gates. All three refined long-podcast extraction children passed; 35/86 extraction tasks now cover 2610/6303 original segments. Further live validation is in progress; no full-episode quality, formal-course or 256K acceptance is claimed, and the stable installation/main remain unchanged.

## 2026-10-11 旧检查点（历史）

**238 项离线测试通过，真实整集验收未通过。** 所有本轮测试进程已结束，临时调用配置已清理。修复已推送草稿升级分支；GitHub main 与 OpenClaw 正式安装仍为原版本。

| 样本 | 已通过 | 当前阻塞 |
| --- | --- | --- |
| 《灵动岛》 | 提取完成；部分正文已缓存 | 写作代际 `e7a82d174cf3e1b6d66c`，3 次后 blocked；最后返回不完整 JSON |
| 《平原上的摩西》 | 16/16 提取块、2 个归纳组 | 第 3 个归纳组三次后 blocked；最后缺少 4 个必需证据编号，最后一组未执行 |
| 《纵横四海 EP86》 | 31/84 提取块，实际双并发 | `extract-0029` 三次后 blocked；最后 `quote_ref=2:0` 不属于其声明的片段集合，52 块未执行 |

三集均未生成本轮最终合格报告，也未标为 completed。复用的历史转录未重新进行 ASR，原资料库及个人笔记未改动。自动重试上限和全部失败状态保留，不以局部分块通过代替报告质量验收。

剩余事项：获得可稳定产生合格证据及报告的模型测试结果，确认正式课程样本文件，并在可用环境中完成窗口验证。已询问 MiniMax M3 的三集材料外发授权，当前尚未收到确认；其模型可用性也未确认。不会将配置窗口或预算模拟宣称为实际容量压力测试。

English: 238 offline tests pass, but no current full-episode acceptance passed. The short writer blocked after three attempts; the interview passed all 16 extraction chunks and two reductions before a reduction blocked; the long episode passed 31/84 chunks before an inconsistent quote reference exhausted three attempts. All processes stopped and private invocation configs were removed. The draft branch is updated; main and the installed Skill are unchanged. Alternative-model authorization, a formal course sample and window verification remain pending.

## 2026-10-10 修复与续测（历史检查点）

**238 项离线测试通过；真实整集最终验收仍进行中，不把局部分块通过当作发布通过。** 正式资料库和 OpenClaw 安装目录未修改，未合并 main。

- 原文审稿改为“原文覆盖批次 + 稿件依据分片”，不再遍历全部交叉组合。两个方向均先保留每条断言最匹配的完整候选，再按证据和完整提示预算分组。完整原句、正文与知识条目均有无遗漏/重复检查；这只是处理完整性，不是事实准确率。
- 独立复查发现并修复：说话人标签丢失、稿件分片所需原文超过预算、添加 ID 后将可分句段落误判为不可分；另补覆盖方向的证据预留回归。匿名说话人不会被推断为真实姓名。
- 审稿问题改为选择提供的 `source_id`，程序恢复对应逐字原文；拒绝未知地址、稿件地址冒充原文地址及模型自行填入替代引文。不改变模型给出的通过/失败结论，也不把这项修复称为语义判断正确。
- 真实审稿曾声称稿件包含实际不存在的女性代词和英文标题。新增错误断言的稿件编号、逐字片段及生产章节绑定，拒绝批评不存在的文字；正文遗漏按全部提供段落合并判断。修复中文姓名正则吞入相邻说明文字的误判，语义审稿要求继续保留。
- 审稿提示和规划版本有指纹。真实变更生成新代际、保留旧状态与完整失败次数；同一输入仍不能清空三次上限。合格写作缓存仍需提示、模型、配置和输出哈希一致。
- 提取保留估算 8K 上限，将完整片段数量上限从 32 改为 96；《纵横四海 EP86》仍保留全部 6303 个片段，规划为 84 块。此为结构性降调用数，不代表 84 块已通过真实模型验证。
- 修复引述时间边界：起点只允许原段起点的秒级四舍五入，不再放宽到附近 8 秒；不允许追加无关后续片段来使过长的结束时间通过。
- 《灵动岛》早期本轮审稿先因自相矛盾的问题清单、错误原文引文定位而被拒绝；后一次调用因执行沙箱无法解析服务地址失败。确认获准联网的环境可解析后继续，旧失败次数保留。没有修改全局模型配置或猜补不完整 JSON。
- 用户已提供课程样本位置 `/Users/djy/Documents/播客总结`。只读检索未确认正式课程，已询问具体课程文件名；历史“目录未提供”不再适用。学习型播客不冒充正式课程。256K 同模型环境仍未验证。
- 《平原上的摩西》16 个完整提取块全部通过。旧归纳首组三次分别因空证据主题、对象后追加字段、不完整 JSON 失败并 blocked；更换为逐条完整 JSON 记录及输入哈希完成标记，旧失败记录保留，16 块继续复用。新协议仍在真实验证，不宣称归纳或整稿已通过。
- 新归纳协议前两组各第二次通过；第三组三次后 blocked，最后响应缺少 `r0`、`r19`、`r1a`、`r1b` 四个编号，不能把缺失完成标记视为单纯格式问题后宣称通过。最后一组未执行，访谈整稿未生成。
- 写作增加逐字采用原文人名拼写、禁止合并不同变体的明确要求。完整单元素对象数组可有日志地解包，保留全部字段；多元素数组、部分 JSON 和不同对象不能通过此兼容处理。
- 已只读查看本机模型：Agnes 3.0/2.5 Flash 标记可用；MiniMax M3 已配置、标称百万窗口，但运行可用性未确认；GPT-5.5 标记不可用。已询问是否授权三份测试材料发送至 MiniMax M3，未获授权前不执行新目的地的内容测试。没有改变日常默认模型。
- 《灵动岛》原样拼写写作代际 `e7a82d174cf3e1b6d66c` 三次后仍 blocked：依次出现单对象数组返回、加入源中没有的 `Rem Koolhaas`、不完整 JSON。后补的单对象数组兼容仅通过离线测试，不将这份整稿称为通过。
- 长播客前 7/84 块通过，已在当前调用结束后恢复双并发继续。未生成该集报告。修复了未生成 output.json 时重试覆盖原始响应/耗时日志的问题，今后每次启动先归档旧调用日志；已丢失的历史耗时不猜补。

English: 238 offline tests pass. All 16 interview extraction chunks and two reduction groups passed; the third group exhausted three attempts with four required IDs missing. The short writer also exhausted three attempts, while 7/84 long-podcast chunks passed and two-worker extraction continues. Source-aware linear auditing, literal artifact binding, source-name guidance and failed-transport log preservation are implemented. Full-episode acceptance remains pending. No formal course is identified. MiniMax M3 use remains unapproved and its availability unconfirmed. The installation and GitHub main are unchanged; 256K is not tested.

## 2026-10-08 修复续测（优先于下方历史记录）

**代码回归 215 项通过；真实整集验收仍未通过，不合并 main 或覆盖 OpenClaw。** 本轮没有修改正式播客资料库、清空 blocked 次数、切换模型或伪造审核成功。

- 已实现哈希引用注册表：4000 个原始证据 ID 的单组模型输入约 196 字节，全部原始 ID 留在本地并可展开回查。测试覆盖嵌套归并、注册表篡改、按依赖拒绝来源交换及重叠观点的遗漏记录。此前“尚未实现引用注册表”的说明仅属于历史状态。
- 独立审稿拆为完整原句，原段时间戳不变；每批最多 8 个主句，原文不超过 8K 字节、完整提示不超过 23K 字节。JSON 模板逐项列出所需 ID，不猜补截断响应。审稿进度绑定实际提示、来源、模型和配置；修改某个稿件分片可以复用未变的审核，不复用已变的分片。
- 修复审稿未完成时轮询可提前进入 ready_to_assemble 的漏洞；缺失、过期或失败的审核均不能完成写作，运行中保持 running，不触发重复派发。
- 增加完整稿件分片的来源核查，禁止所有原文批次都把虚构主张推给“其他未知批次”；候选检索只提供原始完整句，支持不足必须失败。知识文件中的提及不能掩盖正文遗漏。这一新增核查仅完成模拟契约测试，尚未做真实整集验收。
- 未传私有配置的新版写作任务自动使用权限 0600 的临时副本，仅禁止全部工具，保留模型、provider 和窗口设置；成功/失败均删除副本，不改全局配置。显式不安全配置在调用前拒绝。
- 真实《灵动岛》续测使用新句数限制和旧合格写作片段缓存，代际 36f30033151004770905：3 次独立审稿共 286.16 秒、每次提示估算 20523 UTF-8 字节，依次因非 JSON、无原文支持的拒绝定位、虚假的稿件原样片段失败。写作 blocked / 3，0 个审核对通过，未装配新合格稿。源材料是复制的历史转录，未重新 ASR。
- 上述实测进程使用审稿 v2。随后补充的合法 JSON 模板、稿件来源核查、完成状态保护及临时配置默认路径属于 v3 代码修复，215 项自动测试通过，不能冒称已经真实跑通。

剩余验收：新版真实短稿、两小时访谈全稿、长学习播客全稿、正式课程实际目录和同模型 256K 环境。当前全面审核的源批次 × 稿件分片数量随长内容增加；独立规划探针的 800 句、3 分片需 300 次覆盖审核，另外还有稿件核查。必须进一步验证成本/效率，不能以这版结构测试宣称高效长课程支持。正式课程目录仍未提供，256K 仍未实测。

English: 215 offline checks pass. Provenance registry, bounded review verdicts, resumable exact-prompt review, mandatory current audit before writer completion, artifact grounding, and private default writer config are implemented. The latest real short run exhausted three starts with invalid review responses; later v3 improvements have offline coverage only. No stable deployment, main merge, or full long-source quality claim is made.

## 2026-10-08 最新结论

**正式发布仍未通过。** 本轮自动测试目前 159 项通过；真实短播客首次写作通过机械核验，但独立语义审查发现虚构论文作者和重要机制遗漏，已撤销测试作业的 completed 标记，恢复 awaiting_report。随后两次修复分别因重复 JSON 对象和详细小段不足门槛失败，原写作任务已 blocked / 3，未清空次数。独立审查文件保留在隔离测试目录，没有覆盖正式用户资料。

### 本轮实现与实测

- 输入块仍不超过估算 8K；增加最多 32 个完整 primary segments，重叠仍是完整邻接片段。长学习播客变为 197 块，未丢弃原始 6303 个片段。
- 详细总结按连续证据组写小段，完整章节仍使用原门槛。短样本首次写作 12 个独立调用、12 轮交互、1312.84 秒、报告累计 45489 tokens（含缓存），最大自定义提示估算 17835 UTF-8 字节。不能据此宣称更快、免费或已测试 128K 容量边界。
- 修正预算定义：观点/案例/数字/限制等结构化内容保持 6K 字节上限，带原样引述和定位信息的完整证据文件另限 8K；这是明确的预算定义变化，不把旧 6K 全文件标准下的失败称为旧标准通过。
- 长样本首块旧输出仅因总预算失败；新标准重新核验同一输出通过，保留两次尝试，不调用模型补造内容。第二块真实调用通过，119.76 秒，提示估算 5517 字节。目前只证明前两个新块，不能声称 197 块或整集总结已通过。
- 相同输入或仅预算定义变化时保留原尝试次数；生成代际变化前必须等待运行任务结束。新增迁移、计数和运行保护回归测试。
- 精确定位原文引述，不用附近时间容差猜其起始段。改为完整原句/原段边界摘录；原句改进尚未作为一份最终语义通过报告验证。报告注明分段起点不代表句内精确起点。
- JSON 仅容许有日志的一枚多余末尾右括号、或者完全相同的重复完整对象去重；拒绝不完整对象、不同的第二对象及重复字段。不猜补或丢弃不同证据。
- 独立语义拒绝绑定输入和产物哈希。可疑源外复合专名要求独立核查；合理译名可由绑定当前产物的审核明确批准，此检查不是完整人物关系识别器。测试结果开启 require_semantic_review 后，没有当前通过审查的文件不能完成。
- 增加已完成作业在后续核验失败时退回待总结的逻辑，以及索引按实际作业状态覆盖“有报告即完成”的误判。没有修改 OpenClaw 安装目录。

### 剩余阻塞

1. 源语义保真：首次短稿把库哈斯的论文归给源中不存在的扎哈·哈迪德，加入无依据解释，漏掉自由/自囚、注意力遮蔽、幻想替代现实和不可化解矛盾。机械引述匹配不能代替语义审查；尚未获得本轮最终合格稿。
2. 引用元数据膨胀：本地纯结构探针中，500 个最短格式原始证据 ID 的重复引用已需约 19122 字节；1000 个约 38122 字节，尚无正文。当前 18K 有效整合预算无法消除这一下界，需要引用注册表/分组引用而非继续重复全量 ID；尚未实现该改进，未支付大量模型调用后再假称长课程通过。
3. 两小时访谈尚未完成全稿复测；正式课程实际样本目录仍未提供；同模型 256K 环境仍未实测。

以上记录为当前结论；下文是历史过程。代码仅在升级分支/草稿 PR 更新，不合并 main、不覆盖 OpenClaw、不创建正式发布标签。

日期：2026-10-05。当前状态：148 项离线测试通过；旧短播客真实文件流程重新核验通过。修复后的访谈前五块通过；学习型长播客两块通过，首块三次仍超预算并停止。新短播客逐章写作详细总结三次未达门槛，已停止；课程样本路径仍缺，256K 未实测。修复代码推送升级分支，不合并 main、不覆盖 OpenClaw、不发布正式版。下面保留历次失败及修复记录，不能将历史数据当作当前实现的完整验证。

## 最新修复回归

- 提取适配器由抄写原话改为选择片段序号和原文摘录地址，由脚本恢复稳定 ID 和原样文本。兼容精确原文摘录，但不允许改字、模糊匹配或拼接不相邻原话。
- 自动片段 ID 缩短为序号与文本短哈希；完整 source/chunk/input 哈希继续保护来源和复用。学习型播客由 102 块降至 84 块，6303 个原始片段无删减。84 个提取完整消息最大保守估算 9706 字节，均在 10K 上限内；这不是模型窗口容量实测。
- 写作拆成独立章节、知识洞察和基于完整草稿的覆盖检查批次。引述区机械生成。合格章节按请求哈希缓存和复核，失败部分保留诊断；单个写作任务仍只允许三次启动。
- 新代际自动归档旧状态和完整失败次数。没有将旧 blocked 作业重标为成功；实质输入契约/片段标识变化产生新的隔离回归代际。
- 修复绝对结果路径核验错误更新默认库索引的问题。回归测试验证索引仅更新对应资料库；正式用户库没有被修改。
- 真实访谈回归：前五块已通过，一块在第二次尝试通过，其余首次通过；未处理其余十一块，未生成新的整集总结。输入估算 5613–9783 字节；已完成调用均确认 Agnes 3.0 Flash。
- 真实长播客回归：最新版第 1、2 块通过，首块依次因缺少字段、7044 字节和 7222 字节超预算失败，最终 blocked，保留原输出及日志。其余 81 块未执行，不能宣称长内容完整通过。
- 测试私有配置改为单个模型条目的 contextTokens=131072；最新日志没有旧配置被删除警告。仅执行小输入，不声称压力测试覆盖 128K 边界。
- 新增 21 项纯规划测试及逐章缓存恢复测试。模拟调用只证明调度行为，不证明真实模型质量。
- 独立审查后进一步修复：最终错误按相关章节/覆盖/知识选择失效；缓存绑定模型及配置哈希并核对输出哈希；写作请求预留 1K 修复空间；覆盖行需说明具体理由；适配器要求六条实质洞察。相关恢复行为由离线模拟验证，不冒充这些修复已完成真实整集复测。
- 新短播客隔离副本：提取首次通过，84.79 秒，完整提示估算 5909 字节；内容摘要、内容大纲、核心观点三个章节通过并缓存。详细总结依次返回 527、685、497 个可见字符，低于 700 门槛，写作三次后 blocked，未组装报告或知识文件。首次写作四个独立调用合计 334.66 秒，后两次仅重试详细总结，分别 104.96、82.28 秒；没有重做前三章。这证明局部恢复机制，但不证明完整报告质量通过。
- 该真实进程载入的是最后缓存审查前的适配器；审查修复和将章节目标提高到门槛的 1.8 倍仅有离线回归，未重启被阻断的任务冒充新实测。三次计数和全部日志保留。
- 剩余阻塞：当前模型仍不能稳定满足证据预算和章节详细程度。后续需验证更可靠的有界写作/自检协议，而非降低门槛、清空计数或把退出码当成成功。正式课程样本需实际目录；256K 需同模型可用环境。

## 已验证

- 当前 148 项离线测试通过，包括原有回归测试。
- 完整 segments 分块、元数据预算、稳定 ID、重叠不重复计入正文证据。
- 引述原文及时间定位、数字小数点不能被归一化吞掉、重复原话的时间定位。
- 24K 预算触发分层、原始证据 ID 保留、缺失覆盖与无理由遗漏拒绝。
- 并发上限 2、初次加最多两次重试、断点恢复、上游变化使下游失效。
- 源文变化时复用未变且已核验的块，不跨流程版本复用证据。
- 日期、图片、相对链接、带空格和括号的路径、个人笔记保留、旧结果兼容。
- 合成数据通过真实装配器及最终核验器；该项不使用模型，也不证明总结语义质量。
- Python 编译、Skill 格式、发布版本一致性、Git 空白检查通过。
- 工作会话使用返回的 workspace；写作参考文档位于该目录内，原文按证据 ID 回查。
- 子任务提交前自检；错误会指出具体证据 ID 或实际超预算量。
- 正文与知识洞察不允许编造比原 segments 更精细的时间点；正文链接必须来自转录。

## 历史样本隔离检查

仅复制资料到独立测试目录，没有覆盖原库总结，也没有重新下载音频。

| 样本 | 分钟 | 原 segments | 新分块 | 最大输入估算 |
| --- | ---: | ---: | ---: | ---: |
| 短播客 | 4.31 | 3 | 1 | 4113 |
| 长访谈 | 121.09 | 85 | 16 | 7769 |
| 学习型长播客 | 215.26 | 6303 | 102 | 7856 |

估算采用 UTF-8 字节数保守上界，包含分块的序列化元数据，不包含宿主系统提示和工具开销。三份样本的 primary segments 顺序与文本均无遗漏。最长样本并非正式课程，不能代替课程实测。

另一个旧长节目存在单段超过预算的情况，程序明确拒绝，未拆坏原始 segment 或伪造时间戳。无可靠时间戳的官方 Transcript 也会保留资料并停在待对齐阶段，不能宣称总结已完成。

## 真实模型测试

已读取本机 OpenClaw 模型列表：默认 `agnes-ai/agnes-3.0-flash`，标称窗口 131072，配置 `contextTokens=65536`。没有列示可直接验证的 256K 模型。

用户确认授权后，实际执行了同一短播客的独立任务。每次均为新 OpenClaw headless 会话，模型响应记录确认 provider 为 `agnes-ai`、model 为 `agnes-3.0-flash`；本轮实测并发为 1。

| 阶段 | 尝试 | 耗时（秒） | 结果 |
| --- | ---: | ---: | --- |
| 提取 | 1 | 140.26 | 引述及分块覆盖检查通过，输出超预算，拒绝完成 |
| 提取 | 2 | 210.01 | 测试进程超时，留下的输出仍未通过检查 |
| 提取 | 3 | 204.91 | 子任务自检及主流程检查通过，3 个原始 segments 全覆盖 |
| 写作 | 1 | 462.61 | 草稿存在，但原话混入解释文字，拒绝完成 |
| 写作 | 2 | 216.50 | 更新正文但未提交完整结果；触发上下文压缩，拒绝完成 |
| 写作 | 3 | 559.43 | 完整结果、自检、主流程检查、装配和最终文件核验通过 |

三个完整响应分别报告累计 token 用量 51828、314150、916534（包含缓存读取和多轮请求）。这些是整个代理调用的累计用量，不能解释为单次上下文大小；第二次超时没有完整用量记录。日志里的 cost=0 也不能据此宣称服务免费。

写作草稿的人工检查还发现了编造细时间点、使用 Show Notes 独有链接，以及在过窄工作目录中读取外部参考文档受限。已经修复工作目录、最小写作输入、参考文档位置、自检和相应质量检查；修复后的写作输入估算从 9061 降为 6610。

恢复第二次写作调用时，自动审批再次拒绝，称未识别明确关联测试内容和 Agnes 目的地的用户授权。进程没有启动，未增加写作尝试次数。2026-10-04 用户完整授权了《灵动岛》的转录片段、证据和草稿发送至 Agnes 后，第二、第三次调用均成功启动；未绕过审批或重置次数。

第二次写作日志暴露了模型试图读取工作目录外的 segments 文件，以及约 51576 估算上下文超过本机 49152 预留后的预算。宿主自动压缩后正常退出，但未生成完成标记；不把退出码 0 当作成功。已增加“result 源文件路径仅供本地校验、模型通过证据 ID 定位工作目录内原文”的明确限制和回归断言，107 项测试再次通过。

第二、第三次写作累计用量分别为 312616、1464335 tokens（包括缓存和多轮请求）；第三次共 62 轮交互。单任务输入仅估算 6610，但总交互成本仍高，不能据此宣称低上下文、高效率目标已经全部实现。尚缺逐请求峰值上下文记录；本机实际配置为 64K，不把标称 128K 当作实际 128K 配置测试。

第三次写作产物通过真实装配器与 `--require-report` 最终核验，错误为零。8 条洞察、11 项知识证据、3 条直接引述均通过机械检查；3 张历史归档图片在报告中使用存在的相对链接，原文 URL 保存在 Show Notes，未伪称重新下载图片或保存网页快照。本次使用复制的旧转录，不是重新转录音频。测试 result 无 job_id，证明的是文件最终核验，不是实际后台作业状态转为 completed。

人工对照原始三段检查：词义双关、1972 年论文与柏林墙案例、上海封城回响、飞地与功能筛选、刘海与全面屏、消费新旧关系、集体幻想、技术与欲望的矛盾均覆盖。正文保留 ASR 歧义披露，但“上期转写”措辞不准确，术语区的英文规范化与原样保留声明也不完全一致；这些是仍需改进的语义质量问题，不能仅凭机械通过宣称完美。说话人未确认的警告保留，没有编造身份。

长访谈和学习型长播客目前仅做了结构检查，正式课程、256K 窗口及真实并发 2 仍未验证。本次授权限定《灵动岛》，未向该外部模型发送其他节目。

## 发布闸门

### 本轮续测进展

#### 明确授权后的真实长内容测试

用户明确授权两集内容发送至 `agnes-ai/agnes-3.0-flash` 后，自动审批接受了调用。

- 《平原上的摩西》的旧测试记录缺少 workspace 字段；前两块在调用模型前因本地元数据错误各失败三次。没有转录因此外发，没有将启动错误伪称模型失败。旧计数和失败记录保留，已请求允许另建隔离测试副本，而未自动清空计数。
- 适配器已兼容旧 workspace 元数据；测试调度先检查工作目录与提示预算，再增加启动次数，防止循环消耗本地初始化失败。新增两项回归测试通过。
- 《纵横四海 EP86》真实运行了前两块、全局双并发、指定 Agnes 模型。第一轮均因引述不匹配失败；第二轮分别因引述不匹配和输出 8859 字节超过 6000 上限失败；第三轮分别因另一引述不匹配及不完整 JSON 失败。两块均为 blocked，其他 100 块未继续调用，未组装/发布摘要，也未覆盖原资料。
- 诊断发现返回引述拼接了不相邻原话；增加单段连续引述指引，并修复程序只能检查“被选片段拼接文本”的缺陷。新检查同时要求引述存在于完整 primary 原文中，拒绝通过省略中间内容拼出假原话。新增回归用例通过。
- 第三轮的 JSON 响应在数组中途结束，响应仍报告 ok、stopReason=stop；这不能证明具体 provider 故障原因，但证明必须做结构校验，退出/响应状态不可当成完成。没有猜补缺失 JSON 或伪造证据。
- 反查运行日志发现所有此前测试设置的 legacy `agents.defaults.contextTokens` 被宿主删除。已更正本记录和临时配置构造，改用 Agnes 3.0 Flash 模型条目的 `contextTokens=131072`。未清空失败计数重跑，因此有效 128K 实测依然待完成。
- 正式课程的实际目录路径仍未提供；用户授权句中的“并提供课程样本目录”不是路径，已单独询问。两集外发授权现在已具备，不应再次误报为缺授权。

2026-10-05 另用本地新编的六段虚构软件测试材料完成允许范围内的真实宿主检查，不含任何用户播客/课程内容，不绕过真实长访谈外发拒绝。

| 合成材料阶段 | 启动次数 | 耗时 | 结果 |
| --- | ---: | ---: | --- |
| 提取块 1 | 1 | 142.87 秒 | 原文引述、覆盖与预算通过，1 轮交互，累计 5357 tokens |
| 提取块 2 | 1 | 116.17 秒 | 同批并发，核验通过，1 轮交互 |
| 写作 | 1 | 131.49 秒 | 完整文件提交，但摘要未达 180 字门槛；累计 8848 tokens |
| 写作局部修复 | 2 | 本地拦截 | 携带整份旧稿时估算 25265 超过 24K，未调用模型，未重置次数 |
| 写作局部修复 | 3 | 137.08 秒 | 只携带待修复章节，仍未达摘要门槛；2 轮交互、累计 9427 tokens，最终 blocked |

两项提取实际并发调度并完成，完整提示估算分别为 8273、6292，当前 Agnes 模型响应确认无切换；临时配置测试结束自动删除，用户全局配置不变。**更正：日志显示旧 `agents.defaults.contextTokens=131072` 被 OpenClaw 删除，必须使用 per-model contextTokens。此前“128K 配置下的小输入实测”的结论无效，只能证明双并发和小输入机制；没有完成有效 128K 容量或边界验证。** 测试配置现已改为单个 Agnes 模型条目的 contextTokens，尚未重新实测。

新测试推动了局部章节替换、只携带失败章节、充分原文引述示例和写作目标余量改进；新增断言验证合格章节与未修改知识文件不进入修复提示、章节替换不能注入其他标题。119 项回归通过。不降低章节标准，不靠补字机械凑数，不把 failed/blocked 状态改为成功，也不使用三次上限之外的模型重试。该合成作业未装配或完成；真实课程及长内容质量仍未验证。单次返回提取有真实机制证据，但轻量写作仍是实验性路径。

2026-10-05 再次只读查看模型列表，当前默认仍为 Agnes 3.0 Flash（可用、标称 131072、日常配置 65536）。列表刷新未覆盖全部 provider；没有已确认可用于本轮同模型 256K 测试的环境。不能将未知可用性、其他模型的标称窗口或模拟预算检查当作 256K 实测。

- 独立测试任务使用真实本地 JobTracker、装配器和 run_verify，发现普通 `require_report=False` 检查会错误完成已装配作业。已修复：只有显式要求报告且最终核验通过才调用完成状态转换。4 项集成测试证明有效报告完成、损坏报告/知识仍待总结、普通文件检查不能完成作业。这些采用合成内容，不冒充真实模型质量验证。
- 新增可选 `scripts/openclaw_summary_worker.py`，通过当前 OpenClaw 模型的一次结构化返回，由宿主落盘并调用同一证据校验器；模型不再需要反复编辑文件。5 项适配器契约测试及任务元数据同步测试通过。尚未声称实测效率提升。
- 对现有短播客 2 个任务、长访谈 16 个分块、长学习播客 102 个分块检查完整适配器提示：最大保守估算分别为 8562、9051、9138。提取原分块仍以 8K 为目标，适配器完整消息另有 10K 上限；归并/写作完整消息上限 24K。这是本地预算检查，不是模型实测。
- 尝试启动长访谈的当前 Agnes 模型、128K、双并发隔离测试时，自动审批拒绝：之前的明确外发授权仅覆盖《灵动岛》，本次“继续验证”不足以授权其他节目内容外发。进程未启动、没有增加尝试次数，也没有绕过拒绝。已请求两份已复制长内容的逐集外发授权。
- 正式课程转录样本尚未获得，已请求本地转录和时间戳目录；长学习播客不能替代正式课程。128K 临时配置测试尚未启动；256K 当前模型无可验证窗口，不能修改其标称限制或换模型来冒充同模型测试。

短播客文件流程已完成，但长内容和轻量写作均存在真实失败。需在保留失败记录和次数的前提下修复并获准开展新的隔离回归，再完成长内容、课程及有效窗口验证。256K 若仍不可用则标注未测。不得通过清除次数重新启动本次已达三次的提取或写作任务。

安装前备份已于 2026-10-03 23:46 完成，备份名 `moc-pocast-listener-before-v4.17.0-20261003-234609`，共 356 个文件，含 SHA-256 清单和本机额外排查笔记。

尚未覆盖 OpenClaw 已安装 Skill，尚未推送或合并 GitHub main，也未创建发布标签。通过剩余验证后再同步经验证版本；如果期间安装文件有变化，更新前再保存新的备份。

## English Summary

122 offline tests pass. The earlier short episode and four local JobTracker integration tests passed. Synthetic concurrent extraction passed, but synthetic writing failed and blocked. CORRECTION: the legacy 128K setting was discarded by OpenClaw, so those runs did not verify effective 128K configuration. After explicit authorization, the interview fixture failed locally due to old workspace metadata; compatibility/preflight checks were fixed without clearing its counters. Both first long-podcast chunks exhausted three attempts with quote mismatches, oversized evidence or incomplete JSON. Remaining chunks were not run, and no failed report was assembled. A new strict check rejects quotations assembled by skipping intervening source segments. Per-model window configuration is corrected but untested; the course path is still missing. Installation and GitHub release remain gated on live-quality and remaining validation failures.
