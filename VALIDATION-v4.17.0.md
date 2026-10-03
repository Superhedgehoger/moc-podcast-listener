# v4.17.0 验证记录 / Validation Record

日期：2026-10-03。状态：本地实现与离线验证完成；真实模型质量验证及正式发布待完成。

## 已验证

- 103 项离线测试通过，包括原有回归测试。
- 完整 segments 分块、元数据预算、稳定 ID、重叠不重复计入正文证据。
- 引述原文及时间定位、数字小数点不能被归一化吞掉、重复原话的时间定位。
- 24K 预算触发分层、原始证据 ID 保留、缺失覆盖与无理由遗漏拒绝。
- 并发上限 2、初次加最多两次重试、断点恢复、上游变化使下游失效。
- 源文变化时复用未变且已核验的块，不跨流程版本复用证据。
- 日期、图片、相对链接、带空格和括号的路径、个人笔记保留、旧结果兼容。
- 合成数据通过真实装配器及最终核验器；该项不使用模型，也不证明总结语义质量。
- Python 编译、Skill 格式、发布版本一致性、Git 空白检查通过。

## 历史样本隔离检查

仅复制资料到独立测试目录，没有覆盖原库总结，也没有重新下载音频。

| 样本 | 分钟 | 原 segments | 新分块 | 最大输入估算 |
| --- | ---: | ---: | ---: | ---: |
| 短播客 | 4.31 | 3 | 1 | 4113 |
| 长访谈 | 121.09 | 85 | 16 | 7769 |
| 学习型长播客 | 215.26 | 6303 | 102 | 7856 |

估算采用 UTF-8 字节数保守上界，包含分块的序列化元数据，不包含宿主系统提示和工具开销。三份样本的 primary segments 顺序与文本均无遗漏。最长样本并非正式课程，不能代替课程实测。

另一个旧长节目存在单段超过预算的情况，程序明确拒绝，未拆坏原始 segment 或伪造时间戳。无可靠时间戳的官方 Transcript 也会保留资料并停在待对齐阶段，不能宣称总结已完成。

## 真实模型测试的阻塞

已读取本机 OpenClaw 模型列表：默认 `agnes-ai/agnes-3.0-flash`，标称窗口 131072，配置 `contextTokens=65536`。没有列示可直接验证的 256K 模型。

真实调用会发送隔离样本的转录片段到该外部模型。自动安全审查在进程启动前拒绝了该调用；已经向用户请求明确授权，未绕过限制。当前没有真实模型用量、耗时、质量或成功报告可报告，不能把上述预算和合成测试说成 128K/256K 实测。

## 发布闸门

待明确授权后完成当前模型的独立任务、写作、装配和最终核验，记录质量与重试。256K 若仍不可用则标注未测。还需真实课程样本验证。

因此尚未覆盖 OpenClaw 已安装 Skill，尚未推送或合并 GitHub main，也未创建发布标签。发布时先备份安装目录，保留安装目录额外文件与音频保留规则，再同步经验证版本。

## English Summary

Local implementation and 103 offline tests pass. Three copied historical transcripts passed bounded-input and segment-coverage checks. A synthetic pipeline also passed the real assembler and final verifier; this is not a model-quality test. Live Agnes execution was blocked before launch pending explicit authorization to send transcript excerpts to that external provider. No 256K model or formal-course live run was verified. Installation and GitHub release remain gated on the remaining validation.
