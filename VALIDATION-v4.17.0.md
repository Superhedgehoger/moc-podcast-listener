# v4.17.0 验证记录 / Validation Record

日期：2026-10-03。状态：本地实现与离线验证完成；真实证据提取通过，详细报告尚未通过；正式发布待完成。

## 已验证

- 107 项离线测试通过，包括原有回归测试。
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

三个完整响应分别报告累计 token 用量 51828、314150、916534（包含缓存读取和多轮请求）。这些是整个代理调用的累计用量，不能解释为单次上下文大小；第二次超时没有完整用量记录。日志里的 cost=0 也不能据此宣称服务免费。

写作草稿的人工检查还发现了编造细时间点、使用 Show Notes 独有链接，以及在过窄工作目录中读取外部参考文档受限。已经修复工作目录、最小写作输入、参考文档位置、自检和相应质量检查；修复后的写作输入估算从 9061 降为 6610。

恢复第二次写作调用时，自动审批再次拒绝，称未识别明确关联测试内容和 Agnes 目的地的用户授权。进程没有启动，未增加写作尝试次数。已向用户请求审查系统可识别的完整授权表述，未绕过该拒绝。

因此真实提取已通过，真实写作、最终装配和完整质量核验尚未通过。没有把失败草稿发布成正式报告。长访谈和学习型长播客目前仅做了结构检查，正式课程、256K 窗口及真实并发 2 仍未验证。

## 发布闸门

待自动审批接受完整授权后，复用已核验的证据继续第二次写作，保留首次写作尝试次数与旧草稿。完成写作、装配和最终核验后继续扩大样本验证；256K 若仍不可用则标注未测。还需真实课程样本验证。

因此尚未覆盖 OpenClaw 已安装 Skill，尚未推送或合并 GitHub main，也未创建发布标签。发布时先备份安装目录，保留安装目录额外文件与音频保留规则，再同步经验证版本。

## English Summary

Local implementation and 107 offline tests pass. Three copied historical transcripts passed bounded-input and segment-coverage checks. Live Agnes extraction passed on its third attempt; the first writer produced a draft that failed citation checks and exposed invented fine timestamps and archive-only URLs. Worker workspace, copied references, minimal inputs and quality checks were fixed. Automatic approval blocked the second writer launch despite the earlier confirmation; no new attempt was consumed. End-to-end report quality, 256K, formal-course behavior and real concurrency two remain unverified. Installation and GitHub release remain gated on the remaining validation.
