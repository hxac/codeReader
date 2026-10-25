# 运行服务与发起推理请求

## 1. 本讲目标

上一讲（u1-l2）我们把 `deepseek-v2-lite` 特性编译了出来。本讲解决「跑起来、发请求、看懂日志」这三件事。读完本讲，你应该能：

1. 说清一份合法的 DeepSeek-V2-Lite 权重目录里必须有哪些文件，以及代码如何「验明正身」：probe 三重门 → Config 深度校验 → 权重清单校验，三层检查各自挡住什么。
2. 用 `cargo run --release --features deepseek-v2-lite -- --model-path models/DeepSeek-V2-Lite`（在工作区根目录执行）以 2-GPU 专家并行（EP）形态启动引擎，并理解「单进程、两块卡、rank0 驱动 / rank1 专家」这一部署形态。
3. 通过 OpenAI 兼容的 `/v1/completions` 接口发请求；在没有 HTTP 服务条件时，会用 `e2e_ep2` 集成测试和 `dsv2_lite_ep2_decode_attribution` 诊断二进制这两条替代路线驱动同一套引擎代码。
4. 把启动日志（config → 权重清单 → rank0/rank1 → EP 后端 → KV 池 → 引擎就绪）和每个请求终结时的 `pegainfer_http_trace` 行，逐行对应到 u1-l1 架构图上的环节。

## 2. 前置知识

本讲会用到以下概念，先用一段话讲清直觉，细节再结合源码展开：

- **模型权重目录（HuggingFace 格式）**：一个文件夹，通常包含 `config.json`（模型结构超参）、`tokenizer.json`（分词器）和权重文件。PegaInfer 不打包权重，`--model-path` 指向哪个目录，就用哪个目录里的模型。
- **safetensors 与 mmap**：safetensors 是一种「头部是 JSON 元数据、正文是裸张量字节」的权重格式，按名即可取张量。代码用 `memmap2` 把文件只读映射进内存（mmap），操作系统按需把字节搬进页缓存——这就是启动日志里 `mmap+deserialize ... in xxx ms` 的含义，也是加载大权重不必先整体拷贝一遍的原因。
- **单文件与分片两种形态**：小模型常是单个 `model.safetensors`；大模型按张量切成多个分片，外加一个 `model.safetensors.index.json` 索引，记录「张量名 → 所在分片文件名」。dsv2-lite 的加载代码两种都支持。
- **tokenizer.json**：HuggingFace 的分词器格式。仓库通过 `vllm-text` crate 的 `HuggingFaceTokenizer` 读它，把 prompt 文本编码成 token id 序列（`encode`），把生成的 token id 解码回文本（`decode`）。推理引擎内部只认 token id，不认字符串。
- **CUDA 设备序号（device ordinal）与 `CUDA_VISIBLE_DEVICES`**：每块 GPU 在进程里有一个从 0 开始的序号。`CUDA_VISIBLE_DEVICES=2,3` 会让进程只看见物理上的 2、3 号卡，并把它们重编号为 0、1。dsv2-lite 硬性要求「恰好两个互不相同的序号」，所以用这个环境变量可以挑选哪两块卡参与 EP。
- **OpenAI `/v1/completions`**：OpenAI 定义的补全（completion）HTTP 接口：POST 一个 JSON（含 `prompt`、`max_tokens` 等），服务端逐 token 生成，可流式（SSE）可非流式返回。PegaInfer 对外承诺兼容这个接口（见 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md)），协议翻译由 `pegainfer-frontend` 的 vllm 协议栈承担（u2-l2 精读）。
- **日志级别**：代码用 `log` crate 打日志，启动流水线的关键节点都是 `info!` 级（`debug!` 仅一处加载计划明细）。要让这些行出现在终端上，需要日志初始化把级别开到 info——具体由 server 二进制完成，常见做法是 `RUST_LOG=info`（该环境变量在本仓库的实际效果：待确认，以真实启动输出为准）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `pegainfer-deepseek-v2-lite` crate 内：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs) | crate 门面：probe、`start_engine`、`launch` | 模型线如何声明「固定 EP2 拓扑」 |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model_line.rs) | `ModelLine` trait 实现，server 的派发接缝 | probe / launch 两个回调 |
| [src/engine.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/engine.rs) | 把 generator 包装成 `EngineHandle` | 31 行的完整启动链 |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/config.rs) | 读取并校验 `config.json` | Lite 专属的深度校验 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/weights.rs) | 权重清单与两 rank 的加载计划 | 单文件/分片两条路径、清单校验 |
| [src/model.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model.rs) | rank0（驱动）/ rank1（专家）模型加载 | 每条 rank 的分片加载日志 |
| [src/ep.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/ep.rs) | 专家并行布局 | 64 专家怎么劈成 32+32 |
| [src/runtime/backend.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/runtime/backend.rs) | EP 后端选择（host-staged / nccl） | 环境变量与设备校验 |
| [src/runtime/generation.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/runtime/generation.rs) | `DeepSeekV2LiteEp2Generator` 的加载与生成入口 | 启动日志流水线的每一行 |
| [src/scheduler.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/scheduler.rs) | `MixedRequestScheduler`：多请求批处理 | 请求准入与 `pegainfer_http_trace` |
| [src/scheduler/trace.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/scheduler/trace.rs) | 请求生命周期轨迹 | 每请求一行日志的字段清单 |
| [src/bin/dsv2_lite_ep2_decode_attribution.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/bin/dsv2_lite_ep2_decode_attribution.rs) | 无 HTTP 的 EP2 诊断基准二进制 | CLI 参数与 SHA 精度门 |
| [tests/e2e_ep2.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/tests/e2e_ep2.rs) | EP2 端到端集成测试 | `GenerateRequest` 的完整字段证据 |

## 4. 核心概念与源码讲解

### 4.1 权重目录准备：模型目录里必须有什么

#### 4.1.1 概念说明

server 进程拿到的只是一个目录路径。它要依次回答三个问题：

1. **这是不是我负责的模型线？**——读 `config.json` 的少量字段做轻量探测（probe）。
2. **这份配置我撑不撑得起？**——把 `config.json` 反序列化成 `Config`，逐字段深度校验（层数、头数、hidden size……）。
3. **权重文件齐不齐、放没放对？**——扫描 safetensors 文件建立「张量名 → dtype/shape」清单，再按两个 rank 的加载计划核对。

任何一层不过，启动都会在日志里留下明确的报错并退出。所以「准备权重目录」本质上是让这三层检查全部通过。
