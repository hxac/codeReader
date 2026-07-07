# 贡献与 QA 工作流

## 1. 本讲目标

本讲是 ds4 学习手册的收尾篇。前面十几个单元拆开了引擎、模型加载、推理、采样、四大 GPU 后端、服务器、磁盘 KV、SSD 流式、分布式、agent、评测、基准、GGUF 工具链与测试向量。本讲不再讲任何新算法，而是回答一个工程问题：

> 当你真的动手改了 ds4 的代码，凭什么相信它「还是对的、还是快的」？

读完后你应该能够：

1. 说清 ds4 贡献流程的两条回归轨道——**正确性（`ds4_test`）与速度（`ds4-bench`）**，以及那条铁律「速度只能为正确性让步」。
2. 拿着 `QA_BEFORE_RELEASES.md` 这份发布闸门，按子系统逐项执行，并知道每条命令保护的是哪条历史易回归路径。
3. 理解 `AGENT.md` 把 **Metal 默认推理、SSD 流式、CUDA、分布式** 四条路径定为「命脉」，改一处不得连累其它三条；并掌握两条硬性安全约束。
4. 为「修改 KV 缓存代码」这类高风险变更，亲手写出一份覆盖四条路径的回归检查清单。

## 2. 前置知识

本讲默认你已经读过手册里关于四大推理路径与测试体系的讲义，关键概念在此一句话回顾，不重复展开：

- **四大推理路径**：同一份 `ds4.c` 引擎核心被四种「后端对象」复用——Metal 图推理（`ds4_metal.m`，生产主路径）、CUDA（`ds4_cuda.cu`）、ROCm/HIP（`ds4_rocm.cu`，Strix Halo）、CPU 参考路径（`-DDS4_NO_GPU`）。这条复用关系由 `Makefile` 的 `CORE_OBJS` 在编译期切换第四个对象实现（详见 u1-l4）。
- **SSD 流式**：把 routed 专家从常驻内存降级为按需 `pread` 的缓存行，是「容量路径」而非默认路径（详见 u9-l1、u9-l2）。
- **分布式**：coordinator/worker 沿路由转发 hidden state，KV 状态用滚动 FNV-1a 前缀 hash 校验（详见 u9-l3、u9-l4）。
- **磁盘 KV 缓存**：`.kv` 文件 = 固定头 + 渲染文本 + DSV4 payload + 可选 KTM 段，是服务器与 agent 用户的高影响面（详见 u8 全单元）。
- **测试向量**：官方向量对齐官方实现（弱）、本地 golden 向量抓 64 个真实 logit（强），严格比对要钉死 `DS4_METAL_PREFILL_CHUNK`（详见 u11-l3）。
- **能力评测 `ds4-eval`**：92 题能力回归套件，追求分数稳定性而非满分（详见 u10-l4）；**速度基准 `ds4-bench`** 测前沿瞬时吞吐而非整段平均（详见 u10-l5）。

如果你对这些还陌生，建议先读对应单元；本讲的命令大多是在调用上述体系。

## 3. 本讲源码地图

本讲几乎不引用 C 源码，而引用四份「工程宪法」类文档与构建脚本：

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| [CONTRIBUTING.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md) | 贡献者指南 | 两条回归轨道、`make test` 子检查、量化质量、速度回归、`--trace` 诊断 |
| [QA_BEFORE_RELEASES.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md) | 发布闸门 | 14 节按子系统组织的发布前检查清单与签字条件 |
| [AGENT.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md) | 工程宪法 / 给 AI 代理的约束 | 四条命脉路径、correctness-before-speed、安全约束 |
| [Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile) | 构建/测试脚本 | `make test` / `make cpu` / `make cuda-regression` 的真实实现 |

一句话定位：`AGENT.md` 讲「为什么这样做、哪些不能碰」，`CONTRIBUTING.md` 讲「贡献者每次 PR 要跑什么」，`QA_BEFORE_RELEASES.md` 讲「发版前要把历史上坏过的路径全过一遍」，`Makefile` 把其中一部分固化为可执行命令。

## 4. 核心概念与源码讲解

### 4.1 贡献回归要求：两条轨道与一条铁律

#### 4.1.1 概念说明

ds4 不是通用 GGUF 运行器，而是为 DeepSeek V4 量身打造的窄引擎（见 u1-l1）。窄意味着「一次只死磕一个模型」，也意味着**正确性是绝对优先级**——一个看似无害的优化，可能让某条路径的 logits 悄悄漂移，而你只有在跑官方向量比对时才会发现。

`CONTRIBUTING.md` 开篇就立下方法论与铁律：

> DwarfStar4 changes should be tested against **the failure mode they can realistically affect**. The project has two regression tracks: **correctness and speed**. — [CONTRIBUTING.md:L3-L6](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L3-L6)

意思是：不要无脑跑全量测试，而要判断你的改动「现实上能弄坏什么」，然后针对性地验证那一类失败。项目把验证拆成两条正交的轨道：

- **正确性轨道**：输出对不对（token 序列、logits 分布、工具调用、服务端逻辑）——用 `ds4_test`。
- **速度轨道**：跑得快不快（prefill/生成吞吐）——用 `ds4-bench`。

而把两条轨道焊在一起的是这条铁律：

> Do not send PRs affecting one or more inference backends without checking if the resulting code is still correct and fast. **The only acceptable regression speed is when an important correctness bug is fixed and it requires some speed penalty.** — [CONTRIBUTING.md:L8-L10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L8-L10)

即：碰了任何推理后端的 PR，必须同时证明「仍然正确」和「仍然快」；唯一允许的速度回退，是为了修一个重要正确性 bug 而不得不付的代价。这条规则与 `AGENT.md` 的「correctness before speed」一脉相承（见 4.3）。

#### 4.1.2 核心流程

一个典型 PR 的验证流程如下：

```text
1. make clean && make              # 在默认后端上构建（macOS=Metal，Linux 需显式选后端）
2. make test                       # 正确性轨道：self-test-extractors + agent_test + ds4_test
   ├─ 改了 API/渲染/SSE/工具/缓存账目 → ./ds4_test --server
   ├─ 改了分词器/模板/注意力/logits → ./ds4_test --logprob-vectors
   ├─ 改了长上下文注意力/MoE 路由    → ./ds4_test --long-context
   ├─ 改了 DSML 工具调用            → ./ds4_test --tool-call-quality
   └─ 改了 Metal 内核数值           → ./ds4_test --metal-kernels
3. 改了量化/GGUF → gguf-tools/quality-testing 对比 avg_nll
4. 改了性能敏感路径 → ./ds4-bench 前后两份 CSV 对比 prefill_tps / gen_tps
5. CUDA 机器上 → make cuda-regression
6. CPU 可移植性 → make cpu（仅编译检查）
7. PR 说明里写下：跑过的命令、机器/后端、模型 quant、任何 notable failure
```

关键在于第 2 步的「按失败模式选子检查」：`CONTRIBUTING.md` 明确列了五个窄检查各自覆盖什么（见 4.1.3），让你不必每次都跑全量。

#### 4.1.3 源码精读

**`make test` 到底跑了什么。** 在 `Makefile` 里，`test` 这个伪目标构建四个二进制，再依次运行其中三个（第四个 `q4k-dot-test` 在构建阶段就自运行）：

```makefile
test: ds4_test ds4_agent_test ds4-eval q4k-dot-test
	./ds4-eval --self-test-extractors
	./ds4_agent_test
	./ds4_test
```
— [Makefile:L234-L237](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L234-L237)

也就是说 `make test` = 评测器的抽取器自检 + agent 单元测试 + 主测试运行器 + Q4_K 点积数值自检（[Makefile:L239-L241](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L239-L241) 在编译完 `test_q4k_dot` 后立即执行它）。注意它**依赖模型与 Metal**（在 Linux 上需先选定 CUDA 后端构建）。

**五个窄检查的覆盖范围。** `CONTRIBUTING.md` 把 `ds4_test` 的子检查与它们守护的失败模式一一对应（这是「按失败模式选检查」的查表依据）：

| 命令 | 守护的失败模式 |
|------|----------------|
| `--server` | 请求解析、聊天渲染、流式、工具调用解析、thinking 控制、磁盘 KV 账目等服务端逻辑——API/渲染改动的最佳快查 |
| `--logprob-vectors` | 本地 token 字节与 top-logprob 切片对照官方向量——抓分词器/模板/注意力/logits 回归 |
| `--long-context` | 从 `tests/long_context_story_prompt.txt` 跑长上下文人名-数字召回——抓注意力与 MoE 路由漂移 |
| `--tool-call-quality` | DSML 工具调用在 fast 与 exact 两条路径上的真实模型行为 |
| `--metal-kernels` | 隔离的 Metal 内核数值检查 |

来源：[CONTRIBUTING.md:L38-L52](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L38-L52)。其中 `--logprob-vectors` 的弱/强向量之分已在 u11-l3 讲透：官方切片只能验证贪婪 token 序列，本地 golden 向量才能发现「token 没变但分布已坏」的漂移。

**量化变更走另一套。** 改 GGUF 或量化不要用 `ds4_test`，而要用 `gguf-tools/quality-testing` 的官方续写打分器，逐 token 比较本地 GGUF 给官方 DeepSeek V4 Flash 续写分配的概率，`avg_nll` 越低越好——[CONTRIBUTING.md:L79-L105](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L79-L105)（机制详见 u11-l2）。

**CUDA 与 CPU 的特殊处理。** CUDA 改动必须在 CUDA 机器上 `make && make cuda-regression`——[CONTRIBUTING.md:L62-L67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L62-L67)。`cuda-regression` 在 Linux 上实际运行 `tests/cuda_long_context_smoke`（[Makefile:L138-L139](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L138-L139)），在 macOS 上则是个只打印提示的桩（[Makefile:L77-L78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L77-L78)）。CPU 后端只要求「至少还能编译」：`make cpu`——但有一条致命警告：

> The CPU backend is a reference/debug path, not the production performance target. Remember that **executing the CPU path on Metal can crash the system because of a kernel bug in macOS.** — [CONTRIBUTING.md:L75-L77](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L75-L77)

即 CPU 只做编译检查，绝不要在 macOS 上真跑大模型——这条安全约束在 4.3 还会以宪法形式再出现。

#### 4.1.4 代码实践：按失败模式选子检查

**实践目标**：体会「针对性回归」而非「全量回归」。

**操作步骤**：
1. 假设你刚改了 `ds4.c` 里采样器的 `min_p` 默认值（一个会影响 logits→token 但不改分词/模板的改动）。
2. 对照 [CONTRIBUTING.md:L38-L52](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L38-L52) 的覆盖表，判断这个改动现实上能弄坏哪一类。
3. 执行 `make clean && make` 后，先跑 `./ds4_test --logprob-vectors`（抓 logits 漂移），再跑 `./ds4_test --long-context`（采样变化可能影响长上下文召回）。

**需要观察的现象**：`--logprob-vectors` 报告的本地 golden 向量 top1 是否仍精确相等、top-k 重叠数与 `max_abs` 是否仍在容差内（详见 u11-l3）。

**预期结果**：若默认值改得合理，贪婪 token 序列不变、分布漂移在容差内；若改坏了，本地 golden 向量会先于官方向量报警。

**待本地验证**：本实践需要模型文件与 GPU；若当前环境无模型，可退化为「源码阅读型实践」——只完成步骤 1-2 的判断，写下你选择的子检查及理由即可。

#### 4.1.5 小练习与答案

**练习 1**：你改了 `ds4_server.c` 里 SSE 流式输出的心跳间隔。该跑哪个窄检查？为什么不是 `--logprob-vectors`？

**参考答案**：跑 `./ds4_test --server`。因为心跳属于服务端逻辑（HTTP/SSE/缓存账目），正是 `--server` 守护的范围；而 `--logprob-vectors` 守护的是分词器/模板/注意力/logits，心跳改动根本不碰这些数学路径，跑它是浪费时间。

**练习 2**：一个 PR 把 Metal prefill 的某算子换成了更快的实现，但 `--logprob-vectors` 报告本地 golden 向量 top1 不再精确相等。按铁律，这个 PR 能合吗？

**参考答案**：不能直接合。它违反了 [CONTRIBUTING.md:L8-L10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L8-L10) 的「必须同时正确且快」——logits 漂移说明正确性破了。除非这个漂移本身是在修一个更重要的正确性 bug（铁律的唯一例外），否则必须让实现既快又与 golden 向量逐位一致。

**练习 3**：为什么 `make cpu` 只做编译检查、`CONTRIBUTING.md` 还专门警告不要在 macOS 上跑 CPU 推理？

**参考答案**：CPU 后端是参考/调试路径，不是生产性能目标（[CONTRIBUTING.md:L75-L76](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L75-L76)）；且在 macOS 上对大映射跑 CPU 推理会触发内核 VM 故障导致系统崩溃（[CONTRIBUTING.md:L76-L77](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L76-L77)）。所以 `make cpu` 只用来保证可移植编译，不用来跑模型。

---

### 4.2 QA 检查清单：发布闸门与签字条件

#### 4.2.1 概念说明

`CONTRIBUTING.md` 面向「每个 PR」，`QA_BEFORE_RELEASES.md` 面向「每次发版」。两者的差别是覆盖面：PR 只需验证「这次改动现实能影响的失败模式」，发版则要把**历史上反复坏过的子系统**全部过一遍。文档开宗明义：

> This is the release gate for DwarfStar. Run it before tagging or pushing a release build. **The goal is not to prove every code path exhaustively; it is to exercise the paths that have historically regressed**: Metal graph inference, CUDA, ROCm, SSD streaming, distributed execution, disk KV cache, server APIs, and the agent TUI/tool state machine. — [QA_BEFORE_RELEASES.md:L3-L8](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L3-L8)

注意这份清单的「针对性」哲学：不是穷举所有路径，而是覆盖伤疤路径（historically regressed）。它还定下两条执行纪律——不要同时跑多个大模型进程、每次手动运行都要记录 commit/硬件/GGUF/上下文/非默认参数（[QA_BEFORE_RELEASES.md:L9-L11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L9-L11)）。

#### 4.2.2 核心流程

`QA_BEFORE_RELEASES.md` 分 14 节，按子系统组织。下面是它们的逻辑分组与对应的伤疤：

```text
§1  构建整洁度        make clean && make / make cpu / git diff --check / --help 渲染
§2  核心回归          make test + --logprob-vectors + --local-golden-vectors + --server + --self-test-extractors
─── 四大 GPU 路径 ───
§3  Metal Flash       一次性 CLI + thinking/max-thinking + 长上下文召回 + --dump-logprobs + ds4-bench
§4  Metal PRO         （实验性）短 prompt 验证模板/thinking/端点别名；无机器则审查形状/张量/KV 兼容
§5  SSD 流式          q2/q2-q4 流式 + 混合 quant 长提示（不得报 "model range not covered"）+ --ssd-streaming-cold
§6  CUDA / DGX Spark  make cuda-spark + make cuda-regression + 短/长 prompt 记录 t/s
§7  ROCm / Strix Halo make strix-halo + q2 imatrix 短 prompt（不得用混合 q2-q4，会系统 OOM）
§8  分布式            worker 先起、coordinator 后起；Ctrl+C 干净退出；KV 快照存取
─── 高影响面 ───
§9  磁盘 KV           同请求二次命中、触发淘汰验锚点保留、不兼容 checkpoint 拒绝、agent /strip 后 /switch 重建
§10 服务器 API        /v1/models 别名 + OpenAI/Responses/Anthropic + SSE thinking 开关 + 长预填保活 + --trace
§11 ds4-agent         斜杠命令全家桶 + 各类 Ctrl+C + 队列消息 + read/edit/bash/web 工具 + 真实编码循环 + TUI
§12 下载脚本          download_model.sh 在临时目录验 URL/续传/命名/软链
§13 性能与功耗        ds4-bench 对比基线 + --power 100 不节流 + --power 50 可见降占空比
§14 签字              Metal Flash 过 + CUDA/ROCm 已测或显式声明未验证 + 磁盘 KV/流式/agent 已演练 + 速度在预期方差内
```

四个 GPU 路径（§3-§7）与分布式（§8）正是 `AGENT.md` 的四条命脉加上 ROCm 这条 CUDA 的「兄弟」。§9-§11 是面向真实用户的高影响面，§14 是最终签字条件。

#### 4.2.3 源码精读

**核心回归（§2）要求显式跑两套向量。** 这是与 `CONTRIBUTING.md` 的关键重叠点，但 QA 把它升格为「任何分词器/模板/KV/内核/量化/渲染改动后都必须显式跑」：

> Run the vector checks explicitly after any tokenizer, template, KV, kernel, quantization, or prompt-rendering change:
> `./ds4_test --logprob-vectors` and `./ds4_test --local-golden-vectors`.
— [QA_BEFORE_RELEASES.md:L48-L51](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L48-L51)

注意这里出现了 `--local-golden-vectors`——u11-l3 讲过的「强向量」，QA 把它与官方弱向量并列要求，说明发布门槛比单次 PR 更严。

**Metal Flash 路径（§3）的 logprob 与速度 sanity。** 这是生产主路径，所以既有正确性探针（`--dump-logprobs`）也有速度探针（`ds4-bench`）：

> Logprob sanity: `./ds4 --nothink --temp 0 --dump-logprobs /tmp/ds4-logprobs.json --logprobs-top-k 20 -p "..."` and inspect that the continuation is sane.
> Speed sanity: run `ds4-bench` with `speed-bench/promessi_sposi.txt` and compare prefill, generation speed, and KV bytes with the last known good numbers for the same machine.
— [QA_BEFORE_RELEASES.md:L68-L74](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L68-L74)

`--dump-logprobs`（u4-l3 的只读旁路）与 `ds4-bench`（u10-l5）在这里被用作发版的「金丝雀」。`--temp 0` 强制贪婪、`--nothink` 关闭思考，都是为了得到确定可对比的输出。

**SSD 流式（§5）的特定错误信号。** SSD 是容量路径，最怕「选中的专家地址落在映射视图之外」这类边界 bug，QA 给了精确的失败信号字符串：

> it must not fail with **"model range is not covered by mapped model views"**
— [QA_BEFORE_RELEASES.md:L95-L97](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L95-L97)

这条混合 quant 长提示测试专门压 prefill 的「选中地址」路径，正是 u9-l2 讲的 expert 寻址表与按需读取的边界。

**ROCm（§7）的机器特定禁忌。** Strix Halo 这台机器的 ROCm 路径在大显存分配时会系统级 OOM 而非干净失败，所以 QA 明令禁止在那台机器上跑混合 q2-q4 / Q4：

> Do not use the mixed q2-q4 or Q4 Flash GGUFs for routine Strix Halo QA yet. They are dangerous on this machine for now because **the ROCm path can hit system OOM instead of failing cleanly.** — [QA_BEFORE_RELEASES.md:L135-L137](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L135-L137)

这是「机器/路径」组合的硬约束，体现了 QA 清单不是抽象流程而是绑定到具体硬件的。

**磁盘 KV（§9）针对服务器用户的高影响。** 它四条检查全部直击 u8 讲过的机制：二次命中、淘汰保留锚点、不兼容 checkpoint 拒绝（model/quant/ctx/raw-compressed KV 布局变化都要拒绝）、agent stripped 会话重建——[QA_BEFORE_RELEASES.md:L163-L175](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L163-L175)。

**最终签字（§14）。** 这是整份文档的收敛点，签字前必须满足：

> - macOS Metal Flash passed.
> - CUDA was tested on the CUDA machine or the release notes explicitly say CUDA was not validated.
> - ROCm was tested on Strix Halo or the release notes explicitly say ROCm was not validated.
> - Disk KV cache was exercised.
> - Server API streaming was exercised.
> - Agent interruption and tool loops were exercised manually.
> - Speed is within expected variance for the same hardware and model.
> - Any skipped item is written down with the reason.
— [QA_BEFORE_RELEASES.md:L242-L253](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L242-L253)

核心思路：Metal Flash 是硬门槛（必须过），CUDA/ROCm 允许「未验证」但必须**显式声明**在发版说明里——绝不许静默跳过。

#### 4.2.4 代码实践：用 QA 清单定位「该跑哪台机器」

**实践目标**：把抽象的发布流程落到具体的机器与命令。

**操作步骤**：
1. 读 [QA_BEFORE_RELEASES.md:L13-L29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L13-L29)，记下三组测试主机：CUDA/DGX Spark（`toor@192.168.0.180`）、Metal/分布式 Mac（`mac-m5max-it`、`mac-m5max-us`，优先 TB5 直连）、ROCm（`strixhalo`）。
2. 假设你要发版且改动了分布式 KV 快照代码。对照 §8（[QA_BEFORE_RELEASES.md:L145-L161](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L145-L161)）列出必须跑的项：worker 先起 coordinator 后起、小 prompt + 长 prompt、Ctrl+C 干净退出、存取分布式 KV 快照。
3. 在 §14 核对：分布式属于 §8，Metal Flash（§3）仍必须过；若你无法访问 CUDA 机器，按 §14 第二条「在发版说明里显式声明 CUDA 未验证」。

**需要观察的现象**：分布式 run 是否「coordinator 等到完整路由后干净退出」、Ctrl+C 是否在「当前分布式 token 或 chunk 排干后」交还控制（[QA_BEFORE_RELEASES.md:L156-L158](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L156-L158)）。

**预期结果**：得到一份「机器 → 命令 → 通过判据」三列清单，能直接交给发版执行者。

**待本地验证**：多机分布式与远程主机访问依赖具体环境；若无，退化为阅读 §8/§14 写出执行计划即可。

#### 4.2.5 小练习与答案

**练习 1**：发版时 Metal Flash 没问题，但你没有 CUDA 机器。按 §14 该怎么办？

**参考答案**：不能假装 CUDA 没事。§14 允许「未验证」，但要求**在发版说明里显式写明 CUDA 未经验证**（[QA_BEFORE_RELEASES.md:L245-L246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L245-L246)）。即「未验证」是允许的状态，但「静默跳过」不允许。

**练习 2**：§5 SSD 流式那条混合 quant 测试，为什么专门要一个「足够长以压到 selected-address prefill 路径」的提示？

**参考答案**：因为 SSD 流式的按需读取（u9-l2）只在 routed 专家被实际选中、且其磁盘地址需要被 `ds4_gpu_stream_expert_table` 寻址时才触发；短提示可能根本不触发 expert cache miss，也就压不到「model range is not covered by mapped model views」这类边界 bug。长提示是为了让 prefill 真正走到选中地址路径。

**练习 3**：§2 为什么在 `make test` 之外还要求**显式**跑 `--logprob-vectors` 和 `--local-golden-vectors`？

**参考答案**：因为 `make test`（[Makefile:L234-L237](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L234-L237)）跑的是默认 `ds4_test`（等价 `--all`），但发版门槛要求在分词器/模板/KV/内核/量化/渲染任何改动后，**额外**把官方向量与本地 golden 向量都显式跑一遍（[QA_BEFORE_RELEASES.md:L48-L51](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L48-L51)），用强向量兜住弱向量抓不到的分布漂移（u11-l3）。

---

### 4.3 路径保护与安全：四条命脉与两条红线

#### 4.3.1 概念说明

`AGENT.md` 表面上是「给 AI 代理的工作笔记」，实质是 ds4 的工程宪法。它把整本书散落的设计取舍收束成几条不可违背的原则，其中对贡献/QA 最关键的是两条：

**第一，四条命脉路径必须彼此不受牵连。** `AGENT.md` 在 Goals 里把它列为与「保持 Metal 全模型图为生产路径」并列的硬目标：

> Always make sure that the **SSD streaming, CUDA, distributed inference, Metal default inference** are not affected by fixes to other parts of the code. — [AGENT.md:L10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L10)

这正是 4.1、4.2 所有测试命令的**根因**：为什么改了一处要跑这么多东西？因为四条路径共享同一份 `ds4.c` 引擎核心（u1-l4 的 `CORE_OBJS` 切换），你在 Metal 上验证「正确」，不代表 SSD 流式、CUDA、分布式也正确。

**第二，correctness before speed。**

> **Preserve correctness before speed.** Do not keep a faster path with unexplained attention, KV cache, or logits drift. — [AGENT.md:L13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L13)

这是 [CONTRIBUTING.md:L8-L10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L8-L10) 铁律的源头：不许保留一条「更快但注意力/KV/logits 有不明漂移」的路径。换句话说，**任何加速都必须能被向量比对解释**，否则就是 slop。

#### 4.3.2 核心流程

`AGENT.md` 的 Testing 节把「四条命脉」落成每次重大改动后的检查顺序：

```text
每次可能影响下列任一项的重大改动后，务必：
1. 测正常 Metal 路径，且速度仍在原水平。
2. 测 SSD 流式路径。
3. 若可能影响分布式，测分布式——但先征得用户同意。
4. 检查 CUDA 是否可能被改坏，并请用户给你 CUDA 机器的访问权以实测。
```
— [AGENT.md:L51-L56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L51-L56)

注意两点：①顺序就是命脉优先级（Metal 默认 → SSD → 分布式 → CUDA）；②分布式与 CUDA 都要求**先问用户**（前者问是否要测，后者要机器访问权），因为它们依赖稀缺的多机/远程资源，不能擅自跑。这与 QA 清单里「需要时向用户要 CUDA 访问权」（[QA_BEFORE_RELEASES.md:L108-L109](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L108-L109)）一致。

`AGENT.md` 还有一条让上述测试**可执行**的设计纪律——公共 API 保持窄、不为语义变体加永久开关：

> Keep public APIs narrow. CLI/server code should not know tensor internals.
> Do not add permanent semantic variants behind flags. Diagnostic switches are fine when they validate the one release path.
— [AGENT.md:L22-L24](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L22-L24)

含义：诊断/校验开关可以临时存在（比如复现官方向量时钉死 chunk 的 `DS4_METAL_PREFILL_CHUNK`，见 u11-l3），但**不允许**留下「永久性的、语义不同的另一条推理路径」藏在 flag 后面——否则四条命脉会裂成八条、十六条，根本测不过来。这条纪律是四路径保护能成立的前提。

#### 4.3.3 源码精读

**四路径共享代码的物理机制。** 之所以「改一处可能连累四条路径」，根源在 `Makefile` 用同一个 `CORE_OBJS` 变量装配所有后端，只换第四个对象：

```makefile
CORE_OBJS = ds4.o ds4_distributed.o ds4_ssd.o ds4_metal.o      # Darwin / Metal
CPU_CORE_OBJS = ds4_cpu.o ds4_distributed.o ds4_ssd.o          # CPU 参考
CORE_OBJS = ds4.o ds4_distributed.o ds4_ssd.o ds4_cuda.o       # Linux 默认 / CUDA
```
— [Makefile:L20-L32](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L20-L32)（节选三处定义）

`ds4.o`、`ds4_distributed.o`、`ds4_ssd.o` 三者是**所有后端共享**的引擎核心。所以你改了 `ds4.c` 里 KV 缓存的任何一行，Metal/CUDA/ROCm 三个图后端 + CPU 参考路径**同时**受影响；改了 `ds4_ssd.c`，SSD 流式路径受影响；改了 `ds4_distributed.c`，分布式受影响。这就是 `AGENT.md:L10` 那条「不得互相牵连」要在源码层守护的现实压力。

而 `strix-halo`（ROCm）目标通过命令行**覆盖** `CORE_OBJS` 的第四个对象与链接器，把 `ds4_cuda.o` 换成 `ds4_rocm.o`：

```makefile
strix-halo:
	$(MAKE) -B ds4 ds4-server ds4-bench ds4-eval ds4-agent \
		CORE_OBJS="ds4.o ds4_distributed.o ds4_ssd.o ds4_rocm.o" \
		CFLAGS="$(CFLAGS) -DDS4_ROCM_BUILD" \
		DS4_LINK="$(HIPCC) $(ROCM_CFLAGS)" \
		DS4_LINK_LIBS="$(ROCM_LDLIBS)"
```
— [Makefile:L107-L112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L107-L112)

这就是四条命脉在构建层的精确切换点（详见 u1-l4、u5-l3）。

**两条安全红线。** `AGENT.md` 的 Safety 节只有两条，但都是硬性：

> - Avoid large CPU inference runs on macOS; the CPU path has previously exposed kernel VM failures with very large mappings.
> - Do not run multiple huge model processes concurrently. **The instance lock is intentional.**
— [AGENT.md:L28-L29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L28-L29)

第一条与 [CONTRIBUTING.md:L76-L77](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L76-L77) 的 macOS CPU 崩溃警告同源（u2-l1 提到的「进程单例锁」就是为第二条服务的）。第二条点明：ds4 故意在引擎打开时获取一个进程级单例锁（见 u2-l1 的 `ds4_engine_open` 副作用），目的就是阻止你同时跑两个大模型进程把机器撑爆——QA 文档也重复了这条（[QA_BEFORE_RELEASES.md:L9-L10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L9-L10)）。

**诊断三件套。** 当四路径里某条出了问题，`AGENT.md` 的注释偏好与 `CONTRIBUTING.md` 的 `--trace` 指引合起来给出排查工具：`--dump-tokens`（分词，u3-l3）、`--dump-logprobs`（logits，u4-l3）、`--trace`（服务端会话，[CONTRIBUTING.md:L138-L144](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L138-L144)）。`AGENT.md` 还要求注释写在实现旁、解释「为什么这个 shape/顺序/缓存边界/内存选择存在」（[AGENT.md:L19-L21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L19-L21)），这让后来者能用源码本身定位回归点，而不必依赖外部设计文档。

#### 4.3.4 代码实践：写出「改 KV 缓存代码」的四路径回归清单

这是本讲义规格指定的核心实践，也是把三个模块串起来的综合练习。

**实践目标**：为「修改 KV 缓存代码」这一典型高风险变更，产出一份覆盖 Metal/SSD/分布式/CUDA 四条路径、可直接执行的回归检查清单。

**操作步骤**：
1. **判定受影响的共享代码**。KV 缓存逻辑主要在共享对象 `ds4.o`（`ds4.c`，见 [Makefile:L20-L32](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L20-L32)）与 `ds4_kvstore.o`（磁盘 KV）。因为 `ds4.o` 被所有后端装配，结论是：**四条路径全部可能受影响**。
2. **按 `AGENT.md:L51-L56` 的顺序排路径**，每条路径从 `QA_BEFORE_RELEASES.md` 与 `CONTRIBUTING.md` 抽出具体命令与通过判据，填入下表：

| 路径 | 必跑命令 | 通过判据 | 来源 |
|------|----------|----------|------|
| ① Metal 默认 | `make clean && make`；`./ds4_test --logprob-vectors` 与 `--local-golden-vectors`；`./ds4_test --long-context`；`./ds4 --dump-logprobs ...` 验 sanity；`./ds4-bench` 对比基线 | 本地 golden 向量 top1 精确相等、长上下文召回正确、prefill/gen t/s 在预期方差内 | QA §2/§3；CONTRIBUTING 正确性+速度轨道 |
| ② SSD 流式 | `./ds4 -m ds4flash.gguf --ssd-streaming --ssd-streaming-cache-experts 32GB -p "..."`；混合 quant 长提示不得报 "model range is not covered"；`--ssd-streaming-cold` 无死锁 | 无 missing expert、无不可能的减速、重复跑同一提示 logprob 一致 | QA §5 |
| ③ 分布式 | worker 先起 coordinator 后起；小+长 prompt；Ctrl+C 干净退出；存取分布式 KV 快照 | coordinator 等到完整路由后干净退出、Ctrl+C 在当前 token/chunk 排干后交还控制 | QA §8（**先征得用户同意**） |
| ④ CUDA | `make clean && make cuda-spark`；`make cuda-regression`；短+长 prompt 记录 t/s | `tests/cuda_long_context_smoke` 通过、t/s 记录在案 | QA §6；CONTRIBUTING CUDA 段（**先要 CUDA 机器访问权**） |

3. **补两条安全红线**（来自 [AGENT.md:L28-L29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L28-L29)）：不要在 macOS 上跑大 CPU 推理（只 `make cpu` 编译检查）；不要同时跑多个大模型进程（单例锁是故意的）。
4. **补签字条件**（来自 [QA_BEFORE_RELEASES.md:L242-L253](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L242-L253)）：Metal Flash 必须过；CUDA/ROCm 若未验证必须在发版说明显式声明；任何跳过项写下原因。

**需要观察的现象**：每条路径跑完后，对照该路径的「通过判据」逐项核对，特别留意 `--local-golden-vectors` 是否抓到 `--logprob-vectors` 抓不到的漂移。

**预期结果**：得到一份与上表同构的清单，其中每条命令都能在本手册前序单元找到机制解释（KV 机制见 u4-l2/u8，SSD 见 u9-1/u9-2，分布式见 u9-3/u9-4，CUDA 见 u5-3，向量见 u11-3）。

**待本地验证**：完整执行需要 Metal Mac + CUDA 机器 + 多机分布式环境。若无，本实践天然退化为「源码阅读 + 文档对照型」——只要步骤 1-4 的判断与表格有据可查即达成本练习目标。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AGENT.md:L22-L24` 禁止「在 flag 后面藏永久性的语义变体」，这跟四路径保护有什么关系？

**参考答案**：四路径保护的前提是「路径数有界、可逐一测试」。如果允许用 flag 永久分叉出语义不同的推理路径，路径数会指数膨胀（4 条变 8 条、16 条），`make test` 与 QA 清单根本覆盖不过来。诊断开关可以临时存在（如复现官方向量时钉死 chunk），因为它校验的是「同一条发版路径」，而不是新增一条并列路径。

**练习 2**：你只改了 `ds4_ssd.c`，没碰 `ds4.c`。还需要跑 Metal 默认路径吗？

**参考答案**：仍然建议至少跑一次 Metal 默认的正确性向量。虽然 `ds4_ssd.o` 不被纯 Metal 路径直接装配进运行时（SSD 是容量路径），但 `AGENT.md:L10` 要求「fixes to other parts 不影响四条命脉」——稳妥起见应用 `--logprob-vectors` 确认默认路径未受牵连；同时 SSD 路径本身（§5）是必跑项。最小集合是：Metal 默认向量快查 + SSD 流式完整测试。

**练习 3**：`AGENT.md:L13` 说「不要保留一条更快但有不明 logits 漂移的路径」。假设一个优化让 prefill 快 10%，但 `--logprob-vectors` 报告漂移、而 `--local-golden-vectors` 的 top1 仍精确相等。该怎么决策？

**参考答案**：不能合。top1 仍相等只说明「贪婪 token 没变」，但 `--logprob-vectors` 报告的漂移说明**分布已经变了**（u11-l3 的核心区分）。`AGENT.md:L13` 明确把「unexplained logits drift」列为禁忌，无论 top1 是否还在。要么解释并消除漂移（让它既快又逐位一致），要么放弃这个优化——除非漂移本身是修更重要正确性 bug 的必要代价（铁律唯一例外）。

---

## 5. 综合实践：为一次「真实的 KV 缓存重构」做发版演练

把本讲三个模块串起来，模拟一次从 PR 到发版的完整回归。

**场景**：你重构了 `ds4.c` 里 compressor frontier 的序列化逻辑（u8-l3 讲的 DSV4 payload），想让磁盘 KV 冷存更紧凑。

**任务**：按下面五步走，产出一份可交付的回归报告骨架。

1. **定位失败模式**（4.1）。重构 frontier 序列化会同时影响：DSV4 payload 的字节布局 → 磁盘 KV 的存取 → 四条路径里凡是要存/取 KV 的都受影响。结论：正确性轨道必跑 `--logprob-vectors` + `--local-golden-vectors` + `--server`（服务器磁盘 KV 账目）+ `--long-context`（frontier 影响长上下文压缩行）；速度轨道必跑 `ds4-bench` 看 KV bytes 列。
2. **跑 PR 级回归**（4.1.3）。`make clean && make` → `make test` → 针对性窄检查。在 PR 说明里写下命令、机器/后端、模型 quant、任何 notable failure（[CONTRIBUTING.md:L3-L6](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md#L3-L6)）。
3. **跑发版级回归**（4.2）。按 QA §2 显式跑两套向量；按 QA §9（[QA_BEFORE_RELEASES.md:L163-L175](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L163-L175)）测：同请求二次命中、触发淘汰验锚点保留、**不兼容 checkpoint 拒绝**（你改了 payload 布局，旧 `.kv` 文件必须被拒绝而不是误加载）、agent `/strip` 后 `/switch` 重建。
4. **守护四条命脉**（4.3）。按 [AGENT.md:L51-L56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L51-L56) 顺序：Metal 默认 → SSD 流式（SSD 也存取专家缓存与 KV，必跑 §5）→ 分布式（分布式有 KV 快照存取，先问用户）→ CUDA（先要机器访问权）。
5. **签字或显式声明**（4.2.3）。对照 [QA_BEFORE_RELEASES.md:L242-L253](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/QA_BEFORE_RELEASES.md#L242-L253)：Metal Flash 过了吗？CUDA/ROCm 测了或显式声明未验证了吗？磁盘 KV 演练了吗？速度在预期方差内吗？跳过项写原因了吗？

**交付物**：一份 markdown 报告，含「受影响路径表 + 每条路径的命令与结果 + 通过/未通过判据 + 签字或未验证声明」。这份报告本身就是 4.3.4 那张四路径清单的真实实例化。

**待本地验证**：完整演练需多机与模型；若环境不全，至少完成步骤 1-2（本机可做）与步骤 3-5 的计划书（文档型）。

## 6. 本讲小结

- **两条轨道 + 一条铁律**：ds4 贡献流程把验证拆成正确性（`ds4_test`）与速度（`ds4-bench`）两条轨道，铁律是「速度只能为正确性让步」——碰了任何后端的 PR 必须同时证明「仍然正确且仍然快」。
- **按失败模式选检查**：`make test` 是默认全量，但 `CONTRIBUTING.md` 给了五个窄检查（`--server` / `--logprob-vectors` / `--long-context` / `--tool-call-quality` / `--metal-kernels`）与各自守护的失败模式，让你针对性地回归。
- **QA 是发版闸门**：`QA_BEFORE_RELEASES.md` 的 14 节不是穷举所有路径，而是覆盖「历史上反复坏过」的子系统，并按四大 GPU 路径 + 磁盘 KV + 服务器 + agent 组织；签字条件允许「未验证」但必须显式声明。
- **四条命脉不得互相牵连**：`AGENT.md` 把 Metal 默认、SSD 流式、CUDA、分布式定为命脉，根因是它们共享同一份 `ds4.o`/`ds4_distributed.o`/`ds4_ssd.o` 引擎核心（`Makefile` 的 `CORE_OBJS`）；改一处要按 Metal→SSD→分布式→CUDA 顺序验证，后两条要先问用户。
- **correctness before speed + 窄 API + 无永久语义分叉**：不许保留有不明 logits 漂移的快路径；公共 API 保持窄；诊断开关可以临时存在但不许留下永久性语义变体——这是四路径可测的前提。
- **两条安全红线 + 诊断三件套**：不在 macOS 跑大 CPU 推理（内核 VM 崩溃）、不同时跑多个大模型进程（单例锁是故意的）；出问题用 `--dump-tokens` / `--dump-logprobs` / `--trace` 三件套自检。

## 7. 下一步学习建议

本讲是手册收尾，没有「下一讲」。建议读者以三个方向收束学习：

1. **把清单用起来**：挑一个真实的小改动（例如给某个推理函数加一行 `--trace` 日志），完整走一遍 4.3.4 的四路径清单，把「读文档」变成「肌肉记忆」。
2. **回读宪法**：带着本讲的框架重读 [AGENT.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md) 全文与 [CONTRIBUTING.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/CONTRIBUTING.md)，你会发现前面所有单元的设计取舍（KV 即一等磁盘公民、窄头 API、压缩 KV frontier 不可廉价回退）都收敛到这几条原则。
3. **向源码要解释**：`AGENT.md:L19-L21` 要求「注释写在实现旁、解释为什么」。下次遇到不解的缓存边界或 shape 选择，直接去 `ds4.c` / `ds4_ssd.c` / `ds4_distributed.c` 找行内注释，而不是找外部设计文档——这是 ds4 留给读者的最后一条学习路径。
