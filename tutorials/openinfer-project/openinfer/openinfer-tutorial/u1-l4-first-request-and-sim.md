# 发出第一个请求与无 GPU 体验（pegainfer-sim）

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `curl` 调用 PegaInfer 的两类 OpenAI 兼容端点：非流式 `/v1/completions` 与流式（SSE）`/v1/chat/completions`。
- 说出 **TTFT** 与 **TPOT** 两个服务指标的定义，并解释 `SimulatedEngineConfig` 如何用三个数字（`base_ttft_ms`、`prefill_tokens_per_ms`、`tpot_ms`）把它们参数化地「模拟」出来。
- 在一台**没有 GPU、没有模型权重**的机器上，用 `pegainfer-sim` 跑通与真实模型完全相同的 HTTP 前端链路（路由 → 分词/模板 → 引擎契约 → SSE 回流），并运行项目自带的前端 e2e 测试。
- 划清 sim 的能力边界：它验证**协议与前端栈**，不验证任何模型精度或真实吞吐。

本讲是单元 1 的收尾：u1-l1 给了你架构地图，u1-l2 让你构建，u1-l3 让你认识 20 个 crate——本讲让你第一次真正「用起来」，并且全程不需要一块 GPU。

## 2. 前置知识

### 2.1 OpenAI 兼容 API 与 SSE 流式

现代 LLM 服务事实上的接口标准是 OpenAI 的 HTTP API。本讲涉及两个端点：

- **`POST /v1/completions`**：文本补全。请求体里 `prompt` 可以是字符串，也可以是**token id 数组**（如 `[1, 2]`）——后者绕过编码，直接把已分词的结果喂给引擎。
- **`POST /v1/chat/completions`**：对话补全。`messages` 数组先经服务端的 chat 模板渲染成一段 prompt 文本，再分词、送引擎。

「流式」（`"stream": true`）指服务端不等全部生成完，而是用 **SSE（Server-Sent Events）** 协议把增量结果一块块推回来：每个事件是一行 `data: {JSON}\n`，事件之间用空行分隔，流的最后以一行 `data: [DONE]` 终止。`curl` 加 `-N` 关闭缓冲，才能实时看到一块块到达。

### 2.2 TTFT 与 TPOT：本讲的两个核心指标

承接 u1-l1 讲过的 prefill/decode 两阶段，服务延迟被拆成两个正交指标：

- **TTFT**（Time To First Token）：从发出请求到收到第一个生成 token 的耗时，主要由 prefill 决定。
- **TPOT**（Time Per Output Token）：之后每多生成一个 token 的平均间隔，由 decode 步节奏决定。

本讲的模拟引擎就是把这两个指标直接当成输入参数。它的 TTFT 模型是：

\[
\text{TTFT} = \text{base\_ttft\_ms} + \frac{\text{prompt\_len}}{\text{prefill\_tokens\_per\_ms}}
\]

即「固定底噪 + 随 prompt 长度线性增长的模拟 prefill 时间」；TPOT 则是一个固定值 \(\text{tpot\_ms}\)。记住这个公式，本讲的实践就是拿尺子验证它。

### 2.3 cargo 操作回顾

- u1-l2/u1-l3 讲过：workspace 的 `default-members` 只有 `pegainfer-server`。所以在仓库根目录运行 sim 必须显式 `-p pegainfer-sim`。
- cargo 参数 `--` 之后的部分才会传给编译出来的二进制（u1-l2 引入的术语）。
- 本讲好消息：`pegainfer-frontend` 的依赖清单里**没有任何 CUDA 相关 crate**，`pegainfer-sim` 只依赖 frontend——所以构建 sim 不需要 CUDA 工具链、不需要 GPU，只需要能联网拉取 git 依赖（首次构建时）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md:L184-L198](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L184-L198) | API 一节：官方 curl 示例（非流式 completions + 流式 chat） |
| [pegainfer-sim/src/main.rs:L1-L65](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/main.rs#L1-L65) | sim 的 CLI 入口：7 个旋钮 → `SimulatedEngineConfig` → `vllm::serve` |
| [pegainfer-sim/src/lib.rs:L1-L430](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L1-L430) | 模拟引擎本体：`SimulatedEngineConfig`、`start_engine`、`SimScheduler` 及单测 |
| [pegainfer-sim/src/logprobs.rs:L1-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/logprobs.rs#L1-L46) | logprobs 协议形状的固定假数据（明确声明不是精度证据） |
| [pegainfer-frontend/src/vllm/mod.rs:L56-L108](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L56-L108) | 前端协议栈的服务入口 `serve` / `serve_with_engine_count` |
| [pegainfer-frontend/src/engine/driver.rs:L20-L104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L20-L104) | `Scheduler` trait 定义与驱动循环（u3 的主角，本讲只看形状） |
| [pegainfer-sim/tests/frontend_e2e.rs:L1-L1034](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L1-L1034) | 无 GPU 的前端 HTTP e2e 套件 + 最小元数据 fixture |
| [pegainfer-sim/tests/tool_call_roundtrip.rs:L1-L80](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/tool_call_roundtrip.rs#L1-L80) | 用「剧本化补全」在纯 CPU 上测工具调用协议往返 |
| [docs/subsystems/frontend/simulated-inference-engine.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/simulated-inference-engine.md) | sim 的官方设计文档：范围、行为、元数据契约 |

## 4. 核心概念与源码讲解

### 4.1 发出第一个请求：OpenAI 兼容 API 与 vllm 服务入口

#### 4.1.1 概念说明

PegaInfer 对外的 HTTP 面完全遵循 OpenAI API：客户端只需把 base URL 指向 `http://localhost:8000/v1`，任何 OpenAI SDK 或 curl 脚本都能直接工作。这个 HTTP 面由 `pegainfer-frontend` 的 `vllm` 模块挂起——它复用 vLLM 的 Rust `vllm-server` crate 承载路由、分词与 chat 模板渲染，把请求翻译成引擎契约，再把引擎产出翻回 OpenAI JSON / SSE。

真实模型线与 sim 共用同一个 `serve` 入口，所以「第一个请求」的体验对两者完全一致；这也是 sim 有资格作为前端测试载具的原因。

#### 4.1.2 核心流程

一次请求在前端侧的粗粒度路径（细节留到 u3-l3/l4）：

```text
HTTP POST /v1/chat/completions
→ vllm-server 路由层（OpenAI schema 校验、chat 模板渲染、分词）
→ 引擎契约（提交给某个 scheduler）
→ 生成 token 逐个回流
→ 增量 detokenize → SSE data: {...} 块
→ 终止：最后一个 chunk 携带 finish_reason，随后 data: [DONE]
```

关键服务语义（来自 `serve` 的文档注释）：

- **边加载边服务**：HTTP 前端（分词器、chat 模板）立即启动，引擎桥在引擎 future 就绪后接入；
- **端口可达即就绪**：HTTP 端口在桥注册完成之后才绑定——所以端口能连上就代表引擎已就绪，客户端不需要额外的就绪探测协议。

#### 4.1.3 源码精读

README 的 API 一节给了两个官方示例：

[README.md:L184-L198](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L184-L198) 说明「把任意 OpenAI 兼容客户端指向 `http://localhost:8000/v1`，两个端点都支持流式」，并给出非流式 completions（`-s`）与流式 chat（`-N`）两条 curl 命令。注意示例里 `"model"` 字段填的是模型路径 `"models/Qwen3-4B"`——服务会把启动时的模型身份原样广告出来。

服务入口的签名与语义在：

[pegainfer-frontend/src/vllm/mod.rs:L56-L82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L56-L82) `pub async fn serve(engine, model_path, served_model_name, port, max_model_len, shutdown)`。文档注释写明两条：引擎以 future 传入，HTTP 先起、桥后接；端口只在桥注册后绑定，因此「reachable port 仍然意味着 engine ready」。参数 `max_model_len` 传 `None` 时会去读 `model_path/config.json` 里的 `max_position_embeddings`。

[pegainfer-frontend/src/vllm/mod.rs:L431-L443](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L431-L443) `resolve_max_model_len`：显式 `Some(n)` 优先；否则读 config；都失败则警告并回退 4096。sim 的 CLI 正是靠传 `Some(max_model_len)` 让一个「没有 config.json 的路径」也能服务——这是 4.2 会用到的伏笔。

#### 4.1.4 代码实践

1. **实践目标**：对任何一台运行中的 PegaInfer 服务（真实模型或本讲后面的 sim），完成第一次非流式与流式调用。
2. **操作步骤**：
   ```bash
   # 先探测服务广告的模型 id（后面请求的 model 字段以它为准）
   curl -s http://localhost:8000/v1/models

   # 非流式补全（token-id prompt，绕过编码）
   curl -s http://localhost:8000/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<上一步查到的id>","prompt":[1,2],"max_tokens":3,"temperature":0.0,"ignore_eos":true}'

   # 流式对话
   curl -N http://localhost:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<id>","messages":[{"role":"user","content":"Write a haiku about Rust."}],"max_tokens":32,"stream":true}'
   ```
3. **需要观察的现象**：`/v1/models` 返回 `data` 数组；非流式响应一次性给出 `choices[0].text` 与 `usage`；流式响应一块块打印 `data: {...}`，首块 `delta` 里有 `"role":"assistant"`，最后一块带 `finish_reason`，然后是 `data: [DONE]`。
4. **预期结果**：流式响应的 Content-Type 是 `text/event-stream`，且 `[DONE]` 恰好出现一次、位于最后——这三点正是 e2e 测试固化的断言（见 [frontend_e2e.rs:L887-L899](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L887-L899) 与 [L901-L943](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L901-L943) 的 SSE 解析器）。若手边暂时没有服务，跳到 4.2 用 sim 起一个。
5. 本实践需要启动一个服务实例；无 GPU 时请配合 4.2 的 sim。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「端口可达即引擎就绪」是个对客户端很友好的性质？它是怎么实现的？

> **答案**：客户端（和测试脚本）只需 TCP 连上端口即可开始发请求，无需先轮询某个 readiness 端点。实现上，[vllm/mod.rs:L56-L63](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L56-L63) 注明 HTTP 监听在引擎桥注册完成**之后**才绑定，所以「能连上」这个事实本身就蕴含「桥已就绪」。

**练习 2**：请求里 `"prompt":[1,2]` 与 `"prompt":"alpha beta"` 有什么区别？

> **答案**：字符串 prompt 要先经服务端分词器编码成 token id；token-id 数组则跳过编码直接进入契约。sim 的官方文档明确建议纯 CPU 测试用 token-id prompt 省掉编码工作——但**生成的 token id 仍要经 detokenize 才能变成文本**，所以元数据里的 `tokenizer.json` 不可省略（见 4.4）。

### 4.2 pegainfer-sim：把一个引擎建模成三个数字

#### 4.2.1 概念说明

`pegainfer-sim` 是一个 **CPU-only 的模拟推理引擎 + 独立二进制**。它的存在回答一个工程问题：前端协议栈（HTTP、OpenAI schema、chat 模板、分词、SSE、指标导出）的测试与压测，凭什么要等一台 8 卡机器？sim 用可配置的 TTFT/TPOT 假装成一个引擎，让 `vllm bench serve` 和前端 e2e 套件在笔记本上就能跑。

先划边界（官方文档 [simulated-inference-engine.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/simulated-inference-engine.md) 的 Scope 一节说得很直白）：

- **模拟**：TTFT 公式（底噪 + 线性 prefill）、固定 TPOT、token id 输出、logprobs 协议形状、spec-decode 计数器。
- **不模拟**：CUDA、内核、KV cache、真实模型执行、 batching 行为、抖动与尾延迟分布。文档明确「不对真实模型吞吐做任何声明」。

sim 是 workspace 成员但不在 `default-members` 里（见 [Cargo.toml:L2-L5](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L2-L5)），所以运行要 `-p pegainfer-sim`。

#### 4.2.2 核心流程

`main()` 一共四步：

```text
clap 解析 7 个 CLI 旋钮
→ SimulatedEngineConfig::new(base_ttft, prefill_rate, tpot, fallback_id)（带合法性校验）
→ start_engine(&config)：spawn 一个 SimScheduler，包成 Engine
→ pegainfer_frontend::vllm::serve(ready(engine), model_id, [], port, Some(max_model_len), ctrl_c)
```

时间模型即 2.2 节的公式。一个请求的完整时间线：

```text
t=0            请求准入（admit），预定首个 token 时刻 = now + TTFT
t=TTFT         第 1 个假 token 发出
t=TTFT+TPOT    第 2 个
t=TTFT+2·TPOT  第 3 个 …… 直到 max_tokens 或剧本放完 → finish_reason
```

#### 4.2.3 源码精读

CLI 旋钮全部集中在：

[pegainfer-sim/src/main.rs:L8-L43](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/main.rs#L8-L43) 定义 `DEFAULT_MODEL_ID = "Qwen/Qwen3-0.6B"` 与 7 个参数：

| 旋钮 | 默认 | 含义 |
| --- | --- | --- |
| `--model-id` | `Qwen/Qwen3-0.6B` | 给前端的分词器/模型元数据 id，**不加载任何权重** |
| `--port` | 8000 | 监听端口 |
| `--max-model-len` | 8192 | 报告给前端的最大上下文（免本地 config.json） |
| `--base-ttft-ms` | 5.0 | TTFT 固定底噪（毫秒） |
| `--prefill-tokens-per-ms` | 100.0 | 模拟 prefill 吞吐，TTFT 线性项的分母 |
| `--tpot-ms` | 12.0 | 相邻假 token 的固定间隔 |
| `--fallback-token-id` | 0 | 空 prompt 请求使用的 token id |

[pegainfer-sim/src/main.rs:L45-L65](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/main.rs#L45-L65) `main()`：组装 config、`start_engine`，然后调 `vllm::serve`——引擎以 `std::future::ready(Ok(engine.into()))` 传入（对 sim 来说「加载」瞬间完成）；`shutdown_token_from_ctrl_c()` 让 Ctrl+C 优雅停机。注意 `engine.into()` 把 `Engine` 转成 `LaunchedEngine::Stepped`（转换实现见 [pegainfer-frontend/src/engine/wiring.rs:L152-L167](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L152-L167)）。

配置结构与校验：

[pegainfer-sim/src/lib.rs:L24-L35](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L24-L35) `SimulatedEngineConfig` 除了四个时序字段，还有两个仅测试使用的成员：`scripted_completion`（逐字重放的 token 剧本）与 `spec_decode`（假装有个 draft 模型）。

[pegainfer-sim/src/lib.rs:L37-L65](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L37-L65) 构造函数用三个 `ensure!` 拒绝非法时序：TTFT 必须 ≥0 且有限、prefill 吞吐必须 >0 且有限、TPOT 必须 ≥0 且有限。对应的单测在 [L399-L406](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L399-L406)，负数、0 吞吐、NaN、无穷大全部被拒。

[pegainfer-sim/src/lib.rs:L91-L97](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L91-L97) 就是 2.2 节公式的代码形态：`ttft(prompt_len)` 返回 `base_ttft_ms + prompt_len / prefill_tokens_per_ms`，`tpot()` 返回固定值。[L100-L111](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L100-L111) 的 `Default` 实现与 CLI 默认值一一对应（5.0/100.0/12.0/0）。

「CPU-only」不是口号，有清单为证：

[pegainfer-sim/Cargo.toml:L7-L21](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/Cargo.toml#L7-L21) sim 的全部运行时依赖只有 `pegainfer-frontend`、`anyhow`、`clap`、`tokio`；而 [pegainfer-frontend/Cargo.toml:L7-L26](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/Cargo.toml#L7-L26) 里是 axum/serde/zeromq 与 `vllm-*` 系列 Rust crate（git 依赖，见 [Cargo.toml:L182-L186](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L182-L186)）——整条链没有任何 CUDA crate。

#### 4.2.4 代码实践

1. **实践目标**：在无 GPU 机器上启动 sim，验证 `--base-ttft-ms` 确实控制首 token 延迟。
2. **操作步骤**：
   ```bash
   # 构造一个最小元数据目录（三个 JSON 的内容照抄 4.4 节的 fixture）
   mkdir -p /tmp/sim-model && cd /tmp/sim-model
   # …写入 tokenizer.json / tokenizer_config.json / config.json（见 4.4）…

   # 慢速启动：TTFT 底噪 3 秒，之后每 200ms 一个 token
   cargo run --release -p pegainfer-sim -- \
     --model-id /tmp/sim-model --port 8000 \
     --base-ttft-ms 3000 --prefill-tokens-per-ms 1000 --tpot-ms 200
   ```
   然后用 4.1.4 的 curl 发一个流式 chat 请求（`content` 用 `"alpha beta"`）。
3. **需要观察的现象**：curl 按下后停约 3 秒才出现第一个 chunk，随后大约每 0.2 秒多一行 `data:`。
4. **预期结果**：按公式，`"alpha beta"` 经该 fixture 分词器得到 2 个 token（`alpha`→1、`beta`→2，见 4.4），TTFT = \(3000 + 2/1000 = 3002\) 毫秒；chunk 间隔 ≈ 200ms（受 4.3 讲的 1ms 停车粒度影响，会有毫秒级误差）。用秒表粗测即可看到 3s + n×0.2s 的节奏。精确测量见第 5 节综合实践。
5. 关于默认 `--model-id Qwen/Qwen3-0.6B`（一个 HuggingFace id）是否需要联网拉取元数据：**待本地验证**；离线环境请一律用本地元数据目录路径，这也是测试套件的做法。

#### 4.2.5 小练习与答案

**练习 1**：把 `--prefill-tokens-per-ms` 从 1000 调到 10，同一个请求的 TTFT 变成多少？

> **答案**：\(3000 + 2/10 = 3000.2\)？不对——注意单位是毫秒：\(2 / 10 = 0.2\) **毫秒**，所以 TTFT ≈ 3000.2ms，几乎没变。这个练习的教训是默认吞吐 100 tokens/ms 意味着 1000 个 token 的 prompt 也只贡献 10ms——想要模拟「长 prompt 显著拖慢首 token」，要把该值调小到个位数。

**练习 2**：为什么 sim 能用 `--model-id` 指向一个**没有 `config.json`** 的路径也能起服务，而真实模型线必须读 checkpoint 的 `config.json`？

> **答案**：sim 显式传 `Some(args.max_model_len)` 给 `serve`，[resolve_max_model_len](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L431-L443) 里显式值优先、根本不读文件；真实模型线则靠 `config.json` 探测模型家族（u2-l1/l2 的主题）。

**练习 3**：`SimulatedEngineConfig::new(0.0, 100.0, 0.0, 42)` 合法吗？它描述了一个什么样的引擎？

> **答案**：合法（校验只要求吞吐为正、其余非负）。它描述一个零延迟引擎：TTFT 与 TPOT 都是 0，token 一下子全部吐完——e2e 测试正是用这组参数让协议断言不被时序干扰（见 [frontend_e2e.rs:L46-L49](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L46-L49) 附近的配置）。

### 4.3 SimScheduler：用三个方法假装自己是一个引擎

#### 4.3.1 概念说明

sim 之所以能「换皮」成引擎，是因为前端只要求引擎实现一个很小的 trait——`Scheduler`（step 驱动契约，u3-l2 会正式精读）。它只有三个方法：

- `submit`：接收一条新请求（只做所有权转移，不表态）；
- `step`：推进一个调度步——准入、发 token、写账本；
- `metrics`：报一份指标快照。

`SimScheduler` 就是这个契约的最小实现：**没有 CUDA、没有 KV cache、没有 LoRA**，一个队列 + 一个运行列表，外加一个把「补全计划」提前算好的函数。理解它等于拿到一份「实现一个 PegaInfer 引擎」的最短样例——项目里还有一份同形状的 `pegainfer-frontend/examples/echo-server.rs`（官方文档提到的姊妹样例）。

#### 4.3.2 核心流程

`step` 的一次调用分两段：

```text
【准入段】drain queued：
  已 abort？→ ledger.retire
  否则 → planned_completion() 一次性算出全部输出 token 与 finish_reason
       → ledger.admit
       → 请求进入 running，首 token 时刻 = now + ttft(prompt_len)

【发射段】遍历 running：
  已 abort？→ retire
  未到时刻？→ 留在 running
  到时刻   → pop 一个 token → ledger.push_tokens
            放完最后一枚 → ledger.finish(reason)
            否则下一枚时刻 = now + tpot()

最后 park_if_waiting()：睡到最近的到点时刻（每次至多 1ms）
```

两个值得记住的设计：

1. **补全在准入时就完全计划好**（输出序列与终止原因都在 admit 时定死），之后的 step 只负责「按时刻放行」——模拟器不需要任何模型状态。
2. **假 token 是 prompt 的循环**：第 \(i\) 个输出 token = `prompt_tokens[i mod prompt_len]`；空 prompt 用 `fallback_token_id`。

#### 4.3.3 源码精读

先看契约本身（u3 的主角，此处只需扫一眼形状）：

[pegainfer-frontend/src/engine/driver.rs:L20-L41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L20-L41) 定义 `Scheduler` trait。注释说明了职责切分：`submit` 只做所有权转移，一切裁决（admit/reject/retire）都在 `step` 里写入账本；`metrics` 每步被驱动循环发布一次。

[pegainfer-frontend/src/engine/driver.rs:L59-L104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L59-L104) `drive` 循环：排空提交通道 → `step` → 发布指标 → 账本 `commit_step`。注意它**从不休眠**——空闲迭代只是 `spin_loop` 提示。这对 GPU 调度器是对的（引擎永远全速），但对纯 CPU 的 sim 会烧满一个核，所以 sim 必须自己在 `step` 里刹车：

[pegainfer-sim/src/lib.rs:L19-L22](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L19-L22) `WAIT_SLICE = 1ms`，注释解释了动机：新请求只在步与步之间被排空，如果一次睡满整个 TPOT 会把准入卡住；1ms 切片既不空转、又把准入延迟压在毫秒级。刹车本体在 [L170-L179](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L170-L179) `park_if_waiting`：取所有运行请求中最近的到点时刻，睡 `min(到点剩余, 1ms)`。

引擎的组装：

[pegainfer-sim/src/lib.rs:L113-L139](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L113-L139) `start_engine` / `start_engine_with_partitions`：每个分区 `spawn_scheduler` 一个专用 OS 线程跑 `SimScheduler`（线程名 `pegainfer-sim-{index}`），`EngineInfo` 的 `kv_capacity`/`servable_len` 都是 `None`（模拟器没有 KV 预算），`lora: None`（`Option` 即能力声明——sim 不提供 LoRA）。`Engine` 结构本身见 [pegainfer-frontend/src/engine/wiring.rs:L132-L140](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L132-L140)。

调度器本体：

[pegainfer-sim/src/lib.rs:L141-L155](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L141-L155) `SimScheduler`（config + queued + running + 可选 spec 计数器）与 `RunningRequest`——注意 `pending` 字段**倒序存放**待发 token，为的是 `pop()` 从尾部取是 O(1)。

[pegainfer-sim/src/lib.rs:L187-L211](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L187-L211) 准入段：调 `planned_completion` 一次性计划输出；若请求要 prompt logprobs，就回一份 `logprobs::prompt` 的假回显；`pending` 为空（如 `max_tokens=0`）立刻 finish。

[pegainfer-sim/src/lib.rs:L213-L250](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L213-L250) 发射段：每个到点的请求**每步最多发一枚 token**；`ledger.push_tokens` 之后，若还有存货则 `next_token_at = now + tpot()`。中间那段 `if let (Some(counters), ...)` 是投机解码扮演：若配置了 drafter，每步都记一次 `observe_draft(k, accepted)`——这让 Prometheus 的 spec-decode 指标在没有 GPU 的情况下也有数据可导出（e2e 断言见 [frontend_e2e.rs:L251-L347](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L251-L347)：4 步 × K=3 → 提案 12、接受 8）。`with_speculative_decoding` 的定义在 [pegainfer-sim/src/lib.rs:L69-L82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L69-L82)。

补全计划与假 token：

[pegainfer-sim/src/lib.rs:L264-L298](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L264-L298) `planned_completion`：无剧本时发 `max_tokens` 枚、终止原因必为 `Length`；有剧本时发 `min(max_tokens, 剧本长)` 枚、**恰好放完整个剧本则终止原因为 `Stop`**。`fake_token_id` 实现循环：`prompt_tokens[index % len]`，空 prompt 回退 `fallback_token_id`——单测 [L356-L362](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L356-L362) 固化了 `[7,9] → 7,9,7` 的行为。剧本模式的用途见 [tool_call_roundtrip.rs:L40-L50](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/tool_call_roundtrip.rs#L40-L50)：把 `<tool_call>{...}</tool_call>` 拆成三段、且断点故意落在 JSON 内部，逼着流式路径跨 chunk 累积一个工具调用。

logprobs 假数据：

[pegainfer-sim/src/logprobs.rs:L35-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/logprobs.rs#L35-L46) `alternatives` 生成 top-k 候选（分数从 -1.0 递减），注释特意声明：**这些分数不是模型精度证据**，只是让 `logprobs` 字段的协议形状有数据可走。读真实 logits 的门禁在 u10-l1。

#### 4.3.4 代码实践

1. **实践目标**：验证 `tpot_ms` 是输出节奏的唯一控制器，并跑通 sim 的单元测试。
2. **操作步骤**：
   ```bash
   # 单元测试（无需网络端口，验证契约形状与时序校验）
   cargo test --release -p pegainfer-sim --lib

   # 用不同 TPOT 各起一次，同一条流式 curl 各发一次
   cargo run --release -p pegainfer-sim -- --model-id /tmp/sim-model \
     --base-ttft-ms 1000 --prefill-tokens-per-ms 1000 --tpot-ms 500
   # 观察后 Ctrl+C，再换 --tpot-ms 100 重启、重发
   ```
   想直接改源码也行：把 [lib.rs:L100-L111](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L100-L111) `Default` 里的 `tpot_ms` 改成 40.0，再 `cargo run --release -p pegainfer-sim`（不带 `--tpot-ms`），CLI 的 `default_value_t = 12.0` 与 `Default` 就此分家——记得观察完改回来。
3. **需要观察的现象**：`max_tokens=6` 的流式请求，`--tpot-ms 500` 时从首 token 到末 token 约 2.5 秒；`--tpot-ms 100` 时约 0.5 秒；文本内容完全相同（假 token 与节奏无关）。
4. **预期结果**：相邻 chunk 间隔 ≈ tpot_ms ± 若干毫秒（1ms 停车切片 + 调度开销）；总时长 ≈ TTFT + (max_tokens−1) × tpot_ms。`--lib` 测试应全绿，其中包括 [L399-L406](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L399-L406) 的非法参数用例与 [L408-L423](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L408-L423) 的「token 循环 + Length 终止」用例（该用例断言 prompt `[7,9]`、max_tokens=3 时输出恰为 `[7,9,7]`）。
5. 本实践无 GPU 要求；首次构建需联网拉取 git 依赖。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SimScheduler::step` 不干脆一次睡满 TPOT，而要 1ms 一片地睡？

> **答案**：[lib.rs:L19-L22](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L19-L22) 的注释：新提交只在**步与步之间**被驱动循环排空（见 [driver.rs:L66-L79](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L66-L79)），睡满 12ms 的 TPOT 意味着新请求最多晚 12ms 才被 admit，压测下 TTFT 会被系统性抬高；1ms 切片是「不空转 CPU」与「准入延迟可控」之间的折中。

**练习 2**：`RunningRequest.pending` 为什么倒序存 token？

> **答案**：[lib.rs:L148-L155](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L148-L155) 注释「stored reversed for cheap pops」：`Vec::pop()` 从尾部弹出是 O(1)，发射段每步都要取「下一个该发的 token」，倒序让这个热操作零拷贝零移动。

**练习 3**：剧本化补全（`with_scripted_completion`）下，`max_tokens=2`、剧本 `[11,22,33]` 会输出什么、以什么原因终止？

> **答案**：输出 `[11,22]`，终止原因 `Length`——`planned_completion` 里 `emit_count = min(2, 3) = 2 < 剧本长度`，没放完剧本就不是 `Stop`。这正是单测 [L381-L396](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L381-L396) 固化的行为。

### 4.4 无 GPU 全链路：元数据契约与 e2e 测试

#### 4.4.1 概念说明

「sim 不加载权重」不等于「什么都不用准备」。前端要构造**正常的** vLLM 文本/对话栈，仍需要三样本地元数据：

| 文件 | 为什么必需 | 缺了会怎样 |
| --- | --- | --- |
| `tokenizer.json` | 生成的 token id 要 **detokenize** 成文本才能进 SSE——哪怕用 token-id prompt 也绕不开 | 启动失败，报「supported tokenizer file … tokenizer.json」类错误 |
| `tokenizer_config.json` | 提供 `chat_template`；chat 端点靠它把 messages 渲染成 prompt | chat 端点无法工作（completions 端点仍可用） |
| `config.json` | 提供 `model_type`、词表大小、`max_position_embeddings` 等元数据 | sim 传了 `Some(max_model_len)` 可容忍缺失；真实模型线则必须存在 |

要点：**没有任何权重文件参与**。这就是「无 GPU 全链路」的全部前提。

#### 4.4.2 核心流程

e2e 测试的服务器装配流程（读者实践时照抄即可）：

```text
tempdir 写入三个 JSON（最小分词器：词表 {"<unk>":0,"alpha":1,"beta":2}）
→ SimulatedEngineConfig::new(0, 1000, 0, 1)（零延迟引擎）
→ start_engine / start_engine_with_partitions
→ vllm::serve_with_engine_count(ready(engine), dir, [模型名], 随机端口, Some(128), N, token)
→ 轮询 /health 直到 200（最多 30 秒）
→ 发请求、断言、CancellationToken 收尾
```

#### 4.4.3 源码精读

元数据 fixture 的构造：

[pegainfer-sim/tests/frontend_e2e.rs:L192-L209](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L192-L209) `model_dir_with_minimal_metadata`：往临时目录写 `tokenizer.json`、`tokenizer_config.json`、`config.json` 三个文件，注释点明「token-id prompt 省掉编码，但生成的 id 仍需分词器 detokenize、外加一个小 config 供元数据」。

三个 JSON 的内容在 [frontend_e2e.rs:L991-L1034](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L991-L1034)：

- `TINY_TOKENIZER_JSON`：一个 **WordLevel** 分词器，词表仅 3 个词（`<unk>`/`alpha`/`beta`），预分词器是 `Whitespace`——所以 `"alpha beta"` 恰好编码为 `[1, 2]`；
- `TINY_TOKENIZER_CONFIG_JSON`：带一个最小 `chat_template`，把每条 message 的 `content` 原样拼接——所以 chat 请求的 prompt 就是用户消息文本本身；
- `TINY_CONFIG_JSON`：`model_type: "pegainfer_sim"`、词表 16（给 logprobs 假候选留空间，见该行注释）、`max_position_embeddings: 128`。

空目录的失败路径也被固化成测试：[frontend_e2e.rs:L756-L781](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L756-L781) 断言「目录里没有 tokenizer.json 时启动必须失败且错误信息点名 tokenizer.json」——元数据契约是**可执行**的，不是文档里的君子协定。

协议形状的断言样例（chat 非流式）：

[frontend_e2e.rs:L542-L610](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L542-L610) 逐字段断言 `object == "chat.completion"`、`role == "assistant"`、content 非空、`max_tokens` 用尽时 `finish_reason == "length"`、以及 `usage` 三项数字自洽（total = prompt + completion）。

流式形状（chat streaming）：

[frontend_e2e.rs:L612-L671](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L612-L671) 断言每块 `object == "chat.completion.chunk"`、首块 `delta.role == "assistant"`、最后一块带 `finish_reason`。最能说明「假 token 循环」的一行在这里：content 为 `"alpha"`（1 个 prompt token）、`max_tokens=2` 时，拼接后的流式内容**恰为 `" alpha alpha"`**——假 token 循环 + WordLevel 解码的可预测产物。

测试文件头部的模块注释也值得读：[tool_call_roundtrip.rs:L1-L17](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/tool_call_roundtrip.rs#L1-L17) 讲清了这个套件测的是「pegainfer 拥有的接缝」：剧本化字节流 → TokenEvent 契约 + detokenize → 上游 Auto 工具解析器 → OpenAI `tool_calls` 输出，非流式与流式各一遍；还坦白了它抓过的两个真实接线缺陷（#584）。

#### 4.4.4 代码实践

1. **实践目标**：亲手构造最小元数据目录，让 sim 在完全离线的机器上跑通 chat 流式端到端。
2. **操作步骤**：
   ```bash
   mkdir -p /tmp/sim-model && cd /tmp/sim-model

   # 1) tokenizer.json —— 照抄 TINY_TOKENIZER_JSON（WordLevel，词表 3 词）
   # 2) tokenizer_config.json —— 照抄 TINY_TOKENIZER_CONFIG_JSON（含 chat_template）
   # 3) config.json —— 照抄 TINY_CONFIG_JSON
   #    源文见 frontend_e2e.rs L991-L1034，逐字符复制即可

   cargo run --release -p pegainfer-sim -- \
     --model-id /tmp/sim-model --port 8000 --base-ttft-ms 2000 --tpot-ms 100

   curl -s http://localhost:8000/v1/models          # 记下广告的模型 id
   curl -N http://localhost:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<查到的id>","messages":[{"role":"user","content":"alpha beta"}],"max_tokens":6,"temperature":0.0,"stream":true}'
   ```
3. **需要观察的现象**：约 2 秒后开始出现 chunk，随后每 0.1 秒一块；每块 `delta.content` 是一个词。
4. **预期结果**：prompt 经模板渲染为 `"alpha beta"` → 分词 `[1,2]`；6 枚假 token 按 `[1,2,1,2,1,2]` 循环 → 拼接内容应为 `" alpha beta alpha beta alpha beta"`（与 e2e 固化的 `" alpha alpha"` 同一机制：每枚 WordLevel token 解码后带前导空格）；最后一块 `finish_reason == "length"`（`max_tokens` 用尽且无剧本），随后唯一一行 `data: [DONE]`。此预测依据源码与既有断言推得，**待本地验证**。
5. 若想更省事，直接运行项目自带的 e2e 套件（同样无需 GPU）：`cargo test --release -p pegainfer-sim --test frontend_e2e`。

#### 4.4.5 小练习与答案

**练习 1**：`TINY_CONFIG_JSON` 里 `"vocab_size": 16`，词表实际只有 3 个词，为什么留 16？

> **答案**：[frontend_e2e.rs:L1029-L1030](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L1029-L1030) 行尾注释：「给模拟的 logprobs 候选（scored id + 1..k）留词表空间」——`alternatives` 生成的候选 id 是 `scored_id + 1` 起，词表太小会造出越界 id。

**练习 2**：chat 模板为什么被要求「至少渲染出一个 sim 能流式输出的 token」？

> **答案**：官方文档（[simulated-inference-engine.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/simulated-inference-engine.md) Frontend Metadata Contract 一节）警告：若模板渲染出的 prompt 为空或没有 token，`delta.content` 永远不出现，响应形状测试会在「没测到任何东西」的情况下通过——假阴性。这是模拟器测试设计的真实陷阱。

**练习 3**：`start_engine_with_partitions(config, 3)` 起的服务，与 `serve_with_engine_count(..., engine_count=2)` 一起用会发生什么？

> **答案**：启动失败。`frontend_rejects_engine_partition_mismatch`（[frontend_e2e.rs:L462-L489](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L462-L489)）断言了错误信息「declared 2 engines but the launched engine exposes 1 schedulers」——对外声明的引擎身份数必须与实际 scheduler 分区数一致（数据并行线的约束，u3 会展开）。

## 5. 综合实践

**任务：在无 GPU 的机器上，给一个「假引擎」量化 TTFT/TPOT，并用源码公式对账。**

1. **准备**：按 4.4.4 构造 `/tmp/sim-model` 元数据目录。
2. **启动慢速引擎**：
   ```bash
   cargo run --release -p pegainfer-sim -- \
     --model-id /tmp/sim-model --port 8000 \
     --base-ttft-ms 3000 --prefill-tokens-per-ms 1000 --tpot-ms 200
   ```
3. **写一个 10 行的 TTFT 测量脚本**（示例代码，仅用标准库；`MODEL` 换成 `/v1/models` 查到的 id）：
   ```python
   import json, time, urllib.request
   body = json.dumps({"model": "MODEL", "messages": [{"role": "user", "content": "alpha beta"}],
                      "max_tokens": 6, "temperature": 0.0, "stream": True}).encode()
   req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions", data=body,
                                headers={"Content-Type": "application/json"})
   t0 = time.perf_counter(); stamps = []
   with urllib.request.urlopen(req) as resp:
       for raw in resp:
           line = raw.decode().strip()
           if line.startswith("data: ") and line[6:] != "[DONE]":
               stamps.append((time.perf_counter() - t0) * 1000)
   print(f"TTFT ≈ {stamps[0]:.1f} ms")
   print("相邻 chunk 间隔:", [round(b - a, 1) for a, b in zip(stamps, stamps[1:])])
   ```
4. **与公式对账**：理论 TTFT = \(3000 + 2/1000 \approx 3002\) ms；相邻间隔应全为 200ms 上下（毫秒级抖动来自 1ms 停车切片与 HTTP 开销）。
5. **改参复测**：`--tpot-ms 500` 重启再跑，间隔应整体变为 500ms 左右，TTFT 不变——确认两个旋钮正交。
6. **并发压力**：用 `seq 1 8 | xargs -P 8 -I{} curl -s ... -d '{...,"max_tokens":4}'` 同时打 8 路，比较各路 TTFT 与单路是否一致；按 sim 的实现（每请求独立定时、无排队）预期基本不漂移——并想清楚这是**模拟器**的性质，真实引擎的并发排队行为要等 u6 的调度器才登场。
7. **收尾**：`cargo test --release -p pegainfer-sim`（lib + 两个 e2e 套件）全绿，即在你这台无 GPU 机器上验证了整条前端链路。

## 6. 本讲小结

- OpenAI 兼容 API 的两个端点（`/v1/completions`、`/v1/chat/completions`）都支持流式；SSE 流以唯一一行 `data: [DONE]` 终止，这些协议形状由 e2e 测试固化。
- `pegainfer-sim` 是 CPU-only 的模拟引擎：`SimulatedEngineConfig` 用 \(\text{TTFT} = \text{base} + \frac{\text{prompt\_len}}{\text{rate}}\) 与固定 TPOT 三个数字建模时序，输出 token 按 prompt 循环（或按剧本重放），不碰任何 CUDA/KV/权重。
- `SimScheduler` 是 step 驱动 `Scheduler` 契约的最小实现：补全在准入时一次计划好，每步每请求最多放一枚 token；1ms 停车切片平衡「不烧 CPU」与「准入延迟」。
- 前端 `vllm::serve` 对 sim 与真实模型完全一视同仁——端口可达即引擎就绪；sim 只需补上三个元数据 JSON（分词器、chat 模板、config），无需任何权重文件。
- sim 的边界同样重要：它验证协议、指标导出与前端栈，不提供任何模型精度或真实吞吐证据。

## 7. 下一步学习建议

单元 1 至此结束——你已经能构建、启动并向服务发请求。下一讲进入单元 2：

- **u2-l1（server 入口：从 config.json 到引擎启动）**：对比 sim 与真实入口的差异——sim 的 `main.rs` 直接调 `vllm::serve`，而 `pegainfer-server` 要经过模型线注册、config.json 探测、参数校验、`launch` 与 `ServePlan` 分发，这正是下一讲的解剖对象。
- **u3-l2（step 驱动的 Scheduler trait 与 RequestLedger）**：本讲你已见过 `Scheduler` 三方法与 `drive` 循环的形状；u3 将精读 `RequestLedger` 的「终结恰好一次」账本、`StepOutputs` 消息与 ZMQ 桥的完整数据通路。
- 继续阅读建议：想看 sim 的设计动机与迁移记录，读 [docs/subsystems/frontend/simulated-inference-engine.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/simulated-inference-engine.md) 及其引用的 `sim-step-contract.md`；想看契约的另一个最小实现，读 `pegainfer-frontend/examples/echo-server.rs`。
