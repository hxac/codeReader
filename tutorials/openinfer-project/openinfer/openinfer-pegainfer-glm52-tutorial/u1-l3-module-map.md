# 源码地图：模块划分与各自职责

> 本讲编号：u1-l3（beginner）。前置：u1-l1（GLM5.2 架构与 crate 定位）；建议同时回顾 u1-l2 已建立的 `ModelLine` 链路、`Glm52MoeTopo` 拓扑枚举与 drafter 三态。

## 1. 本讲目标

学完本讲，你应该能够：

1. 拿到任何一个 GLM5.2 相关的功能名词（前缀缓存、CUDA Graph 预捕获、launch-ahead、EP dispatch、DSpark 草稿……），在 1 分钟内定位到对应源码文件。
2. 说出 `src/` 下五个目录子树（`model/`、`scheduler/`、`weights/`、`oracle/`、`benches/`）与顶层散文件的分工，理解「算子层 / 执行层 / 调度层」三层是怎么落到文件上的。
3. 解释为什么 `oracle`、`contract_tests` 这类测试模块用 `#[cfg(test)]` 与生产代码同树编译，而不是放进独立的 `tests/` 目录。
4. 建立后续所有讲义的阅读坐标系：u2 之后每一讲都会引用本讲的文件地图。

## 2. 前置知识

- **Rust 模块树**：一个 crate 的根是 `src/lib.rs`，用 `mod xxx;` 声明子模块。模块既可以是 `lib.rs` 旁边的同名文件 `src/xxx.rs`，也可以是目录 `src/xxx/mod.rs` 加兄弟文件（此时 `mod.rs` 里再声明 `mod yyy;` 对应 `src/xxx/yyy.rs`）。还有一种少见写法 `#[path = "..."] mod xxx;`，可以在任意文件里把指定路径挂成子模块——`dspark.rs` 用它把 `dspark_slot.rs` 挂进来（见 4.1.3）。
- **`#[cfg(test)]` 条件编译**：标了这个属性的模块只在 `cargo test` 编译时存在，正常构建（`cargo build` / `cargo run`）完全不编译它。这是 Rust 把「单元测试与被测代码放同一文件/同一 crate」的标准手法。
- **`pub(crate)` 可见性**：只对本 crate 内部可见。测试模块如果要调用这些内部函数，就必须与生产代码**同 crate** 编译——这是后面解释「测试为何同树」的关键。
- **三层直觉**（对照 CLAUDE.md 的架构图）：HTTP 请求进 `pegainfer-frontend` 的 `EngineHandle`，再进「每模型一个 crate」的调度与执行层，最底下是 `pegainfer-kernels` 提供的 CUDA kernel。本 crate（`pegainfer-glm52`）内部的文件也顺着这个方向分层：**调度层**决定「这一步喂哪些行」，**执行层**把整步 forward 组装成可捕获的 CUDA Graph，**算子层**是一块块可单独验证的 forward 积木。

## 3. 本讲源码地图

本讲精读四个文件（它们分别给出 crate 门面、worker 协议、引擎循环、模型组装的全貌）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `src/lib.rs` | 2097 | crate 门面：模块树、`Glm52LaunchOptions`、`Glm52MoeTopo`、`Glm52Drafter`、`start_engine()` 启动总装 |
| `src/runner.rs` | 1143 | 每个 GPU 一个 worker 线程 + 跨线程指令协议 `Glm52RankCommand` |
| `src/scheduler/mod.rs` | 1807 | free-running 引擎：`Glm52Engine` 的主循环 `serve_loop` |
| `src/model/mod.rs` | 1789 | `Glm52RankModel`：整步 forward 的组装与步形状常量 |

`src/` 全景（`git ls-files 'src/*.rs' | wc -l` 实测 **54 个文件**，共约 2.7 万行）按族归属如下，本讲第 4 节逐族讲解：

| 族 | 文件 |
| --- | --- |
| 门面与启动 | `lib.rs`、`model_line.rs`、`rendezvous.rs` |
| 配置与权重 | `config.rs`、`weights.rs`、`weights/context.rs`、`weights/load.rs`、`weights/staging.rs` |
| 算子层·共享原语 | `fp8.rs`、`bookend.rs`、`rows.rs`、`scratch.rs` |
| 算子层·注意力 | `mla_front.rs`、`mla_decode.rs`、`indexer.rs` |
| 算子层·层组装 | `layer.rs`、`dense.rs` |
| 算子层·MoE | `moe_decode.rs`、`moe_ep.rs`、`moe_ep8.rs`、`moe_tp.rs`、`prefill_tp.rs` |
| 算子层·投机解码 | `mtp.rs`、`dspark.rs`、`dspark_slot.rs`（+ 仅测试的 `dspark_test_support.rs`、`dspark_smoke.rs`） |
| 执行层 | `model/mod.rs`、`model/build.rs`、`model/step_body.rs`、`model/launch_ahead.rs`、`model/mtp.rs` |
| 调度层 | `runner.rs`、`scheduler/mod.rs` 及其 9 个子模块 |
| 测试 | `oracle/`（9 个文件）、`freerun_probe.rs`、`indexer_smoke.rs`、`scheduler/contract_tests.rs`、`scheduler/testkit.rs`、各文件内联 `mod tests` |
| 基准（Python，src 之外） | `benches/kernel_lab/`、`benches/manifests/`、`benches/tests/` |

## 4. 核心概念与源码讲解

### 4.1 模块树总览

#### 4.1.1 概念说明

`lib.rs` 一共声明 **27 个模块**，其中 **23 个生产模块、4 个仅测试模块**（`dspark_smoke`、`freerun_probe`、`indexer_smoke`、`oracle`，都挂 `#[cfg(test)]`）。这 27 个模块里有 4 个是「目录模块」：`model`、`scheduler`、`weights`（兄弟文件 `weights.rs` 做父模块）、`oracle`，各自再往下分。

把 54 个文件记成一张地图的诀窍是按「一次生成请求怎么流过去」记：

```
HTTP 请求
  → pegainfer-frontend 的 EngineHandle（crate 外）
  → 按 data_parallel_rank 路由进某个 rank 的队列
  → scheduler/mod.rs 的 Glm52Engine::serve_loop   【调度层：这一步喂哪些行】
      ├─ scheduler/admission.rs   入口校验 + 全生命周期 KV 预留
      ├─ scheduler/plan.rs        选 bucket、切 prefill span、选采样行
      ├─ scheduler/slot.rs        每个 slot 的纯数据状态机
      └─ scheduler/graph.rs       CUDA Graph 预捕获与导出
  → runner.rs 的 Glm52RankCommand::Step（跨线程指令）  【执行层：把一步发给 GPU 线程】
  → model/mod.rs 的 Glm52RankModel::decode_step
      └─ model/step_body.rs 的 run_step_body（被 CUDA Graph 捕获的那段代码）
          └─ layer.rs 逐层组装                                       【算子层】
              ├─ mla_front.rs / mla_decode.rs / indexer.rs（注意力）
              ├─ dense.rs（前 3 层）/ moe_ep.rs + moe_tp.rs（MoE）
              └─ bookend.rs（首尾）+ fp8.rs（共享投影原语）
  → TokenEvent 回给 frontend
```

#### 4.1.2 核心流程

- **加载期**：`lib.rs::start_engine`（u1-l4 详解）→ `weights/` 把 checkpoint 切成各 rank 的加载包并搬上 GPU → `model/build.rs` 把驻留张量组装成每层权重 → `scheduler/graph.rs` 预捕获全部 bucket 的整步 CUDA Graph（预捕获序列本身就是 fleet 的会合点）。
- **服务期**：每个逻辑 DP rank 一个引擎线程（`glm52-engine-N`），每个 GPU 一个 worker 线程（`glm52-rank-N`）。引擎永不静默等待：空闲 rank 也以 padding 行全速 stepping，唯一的跨 rank 耦合是每层 MoE 的 DeepEP 集合通信本身。
- **投机期**：drafter 打开时，`dspark.rs` 或 `model/mtp.rs` 的草稿通道在步与步之间或固定链上额外跑草稿轮。

#### 4.1.3 源码精读

[src/lib.rs:L11-L41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L11-L41)
模块树全部声明。可以逐行看：`dspark_smoke`（L15-L16）、`freerun_probe`（L18-L19）、`indexer_smoke`（L21-L22）、`oracle`（L33-L34）四个模块前有 `#[cfg(test)]`；唯一的 `pub mod` 是 `model_line`（L27）——它是 crate 对外（server 组装层）仅有的两个公开面之一，另一个是 `Glm52Drafter` 等类型。

[src/lib.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1-L9)
crate 顶部文档一句话给出全局定位：TP4 replicated、EP4..EP64 free-running；非专家栈全量复制到每个 rank、专家在 H2D 时打进打包布局、每个逻辑 DP rank 一个自治引擎。这段话就是整张地图的图例。

[scheduler/mod.rs:L1-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L1-L35)
`scheduler` 目录模块的开头：模块文档讲透 free-running 设计（无协调者、固定 collective 链、保守协议上界、确定性 padding），然后 L37-L47 声明 9 个子模块（`admission`、`contract_tests`、`graph`、`load`、`mtp`、`offload`、`plan`、`slot`、`testkit`，其中 `contract_tests` 与 `testkit` 是 `#[cfg(test)]`）。注意文档最后一句的设计声明：**per-request 决策放 `slot` 的纯数据转换，admission/step-shape 决策放 `admission`/`plan` 的纯函数**——这解释了为什么 `scheduler/` 拆成这么多小文件。

[model/mod.rs:L78-L86](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L78-L86)
`model` 目录模块的四个子模块声明：`build`（权重提取）、`launch_ahead`（投机回放状态机）、`mtp`（native MTP 服务化驻留）、`step_body`（被捕获的整步 forward 主体）。`model/mod.rs` 本体（1789 行）持有 `Glm52RankModel` 与所有步形状常量。

[dspark.rs:L1011-L1020](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dspark.rs#L1011-L1020)
`#[path = "dspark_slot.rs"] mod slot;` 的写法：把 `dspark_slot.rs` 作为 `dspark` 的子模块挂载，理由写在注释里——「拆出去是为了模块大小预算，子模块能保持对私有字段的访问」。`dspark_test_support.rs` 同理，且带 `#[cfg(test)]`。遇到这种 `#[path]` 声明时，文件名与模块名不对应是刻意的。

#### 4.1.4 代码实践：亲手数一遍模块树

1. **实践目标**：不靠本讲义，用只读命令自己验证「27 个模块、4 个仅测试、54 个文件」。
2. **操作步骤**（在仓库根目录执行）：
   ```bash
   cd pegainfer-glm52
   git ls-files 'src/*.rs' | wc -l          # src 下被 git 跟踪的 .rs 文件数
   rg -n '^mod |^pub mod |^#\[cfg\(test\)\]' src/lib.rs   # lib.rs 的模块声明与测试门
   ```
3. **需要观察的现象**：第一条命令输出 `54`；第二条命令里 `lib.rs` 共 27 行 `mod` 声明，其中 4 处 `#[cfg(test)]` 紧贴在 `dspark_smoke`、`freerun_probe`、`indexer_smoke`、`oracle` 之前。
4. **预期结果**：数字与本讲一致。若未来代码演进导致数字变化，以命令输出为准——这正是「用命令刷新地图」的习惯。
5. 两条命令均为只读，可直接执行；若你的环境没有 `rg`，用 `grep -nE '^(pub )?mod ' src/lib.rs` 替代。

#### 4.1.5 小练习与答案

**练习 1**：`lib.rs` 里唯一的 `pub mod` 是哪个？为什么只有它是公开的？

**答案**：`pub mod model_line;`（[lib.rs:L27](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L27)）。server 组装层需要拿到 `MODEL_LINE: Glm52Line` 静态量来实现 `ModelLine` 分发，所以这一面必须公开；其余 26 个模块都是内部实现细节，用 `pub(crate)` 控制可见性，避免把内部结构泄漏成公共 API。

**练习 2**：`weights.rs` 与 `weights/` 目录同时存在，二者是什么关系？

**答案**：`weights.rs` 是父模块（声明 `mod context; mod load; mod staging;`，见 [weights.rs:L29-L31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L29-L31)），持有 `Glm52WeightManifest`（L208 起）与 `all_rank_load_bundles`（L261 起）等对外类型；`weights/` 下三个文件是子模块：`context.rs`（每 rank 的 CUDA 上下文封装）、`load.rs`（mmap 异步加载）、`staging.rs`（pinned 双缓冲）。这是 Rust 2018 之后的「非 mod.rs」目录模块风格：父模块占同名 `.rs` 文件。

**练习 3**：为什么 `scheduler/mod.rs` 的文档说「per-request 决策是纯数据转换、admission/plan 是纯函数」值得写进模块头注释？

**答案**：因为它直接解释了文件拆分的依据：`slot.rs`、`plan.rs`、`admission.rs` 都不依赖 GPU，可以在 CPU 上用 `contract_tests.rs` + `testkit.rs` 直接测契约（u10-l2 详解）。读源码时看到这种「设计声明 + 文件分工」对应的文档，说明该目录的拆分是有意识的结构决策，不是历史堆积。

### 4.2 算子层文件族

#### 4.2.1 概念说明

算子层是「积木层」：每个文件实现模型某一部分的 forward 数学（MLA 注意力、DSA 索引器、MoE、dense MLP……），输入输出都是显式传入的设备缓冲。它们有三个共同特征，读任何一个文件都能验证：

1. **不感知调度**：不知道 slot、请求、bucket 这些概念，只认「本步 T 个独立行」。
2. **形状来自 `config.rs` 常量**：整个 crate 把几何钉死在编译期（u2-l1 详解），所以算子签名里的维度都是常量。
3. **可被 oracle 单独验证**：每块积木在 `oracle/` 下有对应的 HF 参考实现精度门（4.4 节），这是「算子层与测试同树」的根本原因。

按数据流顺序，算子层分四小组：

| 小组 | 文件 | 一句话职责 |
| --- | --- | --- |
| 共享原语 | `fp8.rs`（750 行） | fp8 e4m3 + 128 块 scale 的投影原语：小 m 走 weight-only GEMV，大 m 走激活量化 + groupwise GEMM；`fp8_mlp_into` 是 dense MLP 与共享专家共用的 SwiGLU 形状 |
| 共享原语 | `bookend.rs` | 夹住 78 层堆栈的两端：embedding 查表、final RMSNorm、lm_head（全部 bf16 非 fp8） |
| 共享原语 | `rows.rs` | `Rows<C>`：用 const 泛型把缓冲列宽变成类型事实，替代约 30 处重复 `ensure!` |
| 共享原语 | `scratch.rs` | 启动期一次性分配的 decode scratch：无逐步分配器流量 + 指针稳定（CUDA Graph 的前提） |
| 注意力 | `mla_front.rs` | MLA 前半：hidden→q_a(2048)→q_b 出 64 头查询；hidden→kv_a 出 512+64 压缩 KV 与 rope 分量 |
| 注意力 | `mla_decode.rs` | MLA 后半：kv_b 预反量化为吸收因子 W_UK/W_UV，FlashMLA/FlashInfer 双后端，o_proj 收尾 |
| 注意力 | `indexer.rs` | DSA 索引器：对 index-K 缓存算相似度，选出稀疏 top-2048 的全局 KV 槽位 |
| 层组装 | `layer.rs`（422 行） | 双 norm 残差布局把上面三块拼成一层；持有 `Glm52KvSlab`/`Glm52LayerCaches`（KV 页布局，u4-l2） |
| 层组装 | `dense.rs` | 前 3 层的 dense SwiGLU MLP（中间维 12288），复用 `fp8_mlp_into` |
| MoE | `moe_decode.rs` | 路由器 noaux_tc（sigmoid 打分 + top-8 + norm_topk_prob）与共享专家、专家库权重表示 |
| MoE | `moe_ep.rs` | EP 路径：DeepEP dispatch/combine 集合通信包住 SM100 DeepGEMM masked grouped GEMM；泛型于 EP 宽度 |
| MoE | `moe_ep8.rs` | 仅 18 行：EP MoE 每层的权重包（router + shared + 本地专家库） |
| MoE | `moe_tp.rs` | TP4 prefill-only 路径：每 rank 持 257 个专家的切片（共享专家折叠在 bank 索引 256）+ NCCL all-reduce |
| MoE | `prefill_tp.rs` | TP4 prefill 执行器：layer-outer 分块，fp8 GEMM 保持大 M，indexer 与 FlashMLA 子平铺 |
| 投机 | `mtp.rs` | native MTP 第 78 层的 bookend 数学（准确性口径，服务化驻留在 `model/mtp.rs`） |
| 投机 | `dspark.rs`（1020 行） | DSpark 草稿通道：5 层 qwen3 骨干 + rank-256 Markov 头，rank-local 无集合通信 |

#### 4.2.2 核心流程

一个 token 行穿过算子层的路径（decode 视角）：

```
token_ids
  → bookend::glm52_embed_into           查表得 hidden[T, 6144]
  → 78 × layer.rs::Glm52DecoderLayerWeights::forward
      ├─ rms_norm(input_layernorm)
      ├─ mla_front（投影+norm）→ indexer（full 层算 top-k / shared 层复用 topk_carry）
      ├─ mla_decode（吸收式稀疏注意力 + o_proj）
      ├─ 残差加 → rms_norm(post_attention_layernorm)
      └─ dense.rs（层 0..3）或 moe_decode 路由器 + 共享专家 + moe_ep/moe_tp 专家
  → bookend::glm52_final_norm_into + glm52_lm_head_into → argmax
```

层内数学与 vLLM 的 `DeepseekV2DecoderLayer` 对齐，唯一的 GLM5.2 分叉是**跨层 top-k 共享**：只有 `full` 层持有 indexer 权重并计算新 top-k，`shared` 层原样复用上一个 full 层的 `topk_indices`（u4-l4 详解）。

#### 4.2.3 源码精读

[fp8.rs:L1-L10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L1-L10)
共享投影原语的定位句：GLM5.2 的每个稠密投影都是 fp8 e4m3 + 每 128 块 `weight_scale_inv`；m=1 时走 weight-only GEMV（免激活量化、免 TMA 重排、一个 kernel 顶三个）。注意最后一行划定了它的服务范围——「只有 EP 路由专家链（多行）留在 grouped CUTLASS 路径上」，这正是 `fp8.rs` 与 `moe_ep.rs` 的分工线。

[layer.rs:L7-L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L7-L16)
层组装的完整数学，五行伪代码写完一层：两次 RMSNorm、一次 MLA(+DSA)、一次 dense/MoE MLP、两次残差加。对照 4.2.2 的流程图，这里就是「层组装」小组存在的全部理由——把注意力与 MLP 积木按官方布局拼起来。

[layer.rs:L18-L23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L18-L23)
跨层 top-k 共享的说明：`full` 层覆写 `topk_carry`，`shared` 层要求它存在；复用之所以成立，是因为索引指向**全局 KV 槽位**而所有层共享同一张块表。

[moe_ep.rs:L11-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep.rs#L11-L18)
EP MoE 的数据流图：dispatch（集合通信，recv 侧 expert-major）→ psum 掩码元数据 → UE8M0 fp8 量化 → DeepGEMM W13 → silu·up 再量化 → DeepGEMM W2 → route_weight 重排 → combine（集合通信）。这张图是 u5 整个单元的目录。

[moe_ep8.rs:L1-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_ep8.rs#L1-L18)
一个完整的「小文件也有明确职责」的样本：18 行只定义 `Glm52MoeEp8LayerWeights`（router + shared + bank 三件套），文档说明了历史（Hopper masked 链已移除，生产 decode 走 `moe_ep` 的 SM100 路径）。

[prefill_tp.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/prefill_tp.rs#L1-L11)
TP4 prefill 执行器的设计声明：layer-outer 让每个层阶段都在 chunk 级大 M 上跑 fp8 GEMM、专家库每 chunk 只读一次；只有 indexer（32 行一批）与 FlashMLA 注意力子平铺。还包含一条因果性论证（先打包整 chunk KV 再注意力为何安全）。

[rows.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L1-L11)
类型化行缓冲的动机：把 `len == tokens * C` 从每个消费点的重复校验搬到构造点，宽度成为类型参数，`[T, HIDDEN]` 传给期望 `[T, VOCAB]` 的地方直接编译失败。这是本 crate 用类型系统固化不变量的代表性手法。

#### 4.2.4 代码实践：验证「dense MLP 与共享专家共用同一原语」

1. **实践目标**：用一条搜索命令验证 4.2.1 表格里「`fp8.rs` 的 `fp8_mlp_into` 同时服务 dense MLP 与 MoE 共享专家」这句话。
2. **操作步骤**：
   ```bash
   rg -n 'fp8_mlp_into' src/
   ```
3. **需要观察的现象**：恰好 3 行命中——`src/fp8.rs:515`（`pub(crate) fn fp8_mlp_into(` 定义）、`src/dense.rs:104`（dense MLP 前向调用）、`src/moe_decode.rs:118`（共享专家前向调用）。
4. **预期结果**：两个调用点说明「共享专家只是中间维更窄（2048 vs 12288）的 dense MLP」——dense.rs 的模块文档也明确写了「reuses the shared `fp8_mlp` helper」。命令只读，可直接执行。
5. 进一步（可选）：打开 [dense.rs:L104-L108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L104-L108) 与 [moe_decode.rs:L116-L119](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_decode.rs#L116-L119)，对比两处调用传入的权重形状参数，体会「形状即差异」。

#### 4.2.5 小练习与答案

**练习 1**：同样是 MoE，为什么 `moe_ep.rs`、`moe_tp.rs`、`moe_decode.rs` 要拆成三个文件而不是一个？

**答案**：三者对应三种执行形态：`moe_ep.rs` 是 EP decode（DeepEP 集合通信 + DeepGEMM，泛型于 EP4..64）；`moe_tp.rs` 是 TP4 prefill-only（每 rank 全部 257 专家的切片 + NCCL all-reduce）；`moe_decode.rs` 是两种形态**共用**的部分——路由器 kernel、共享专家、专家库权重表示。把共用部分抽出独立文件，拓扑差异就不再互相污染。

**练习 2**：`mtp.rs`（算子层）与 `model/mtp.rs`（执行层）为什么是两个文件？

**答案**：职责切分写在 [mtp.rs:L19-L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mtp.rs#L19-L20) 的注释里：`mtp.rs` 只拥有 MTP 独有的数学（`eh_proj` 拼接投影、norm 循环利用的坑），oracle 测试直接调用这些操作；生产服务化所需的驻留与状态（草稿 KV 镜像、CUDA Graph）在 `model/mtp.rs`。「数学」与「驻留」分离，使数学可被 oracle 精度门独立验证。

**练习 3**：`bookend.rs` 为什么强调自己「只是薄封装」？

**答案**：见 [bookend.rs:L4-L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L4-L9)：embedding、final norm、lm_head 三个操作复用 `pegainfer-kernels` 的共享 op，GLM5.2 特有的只有维度；形状不在入口重验，因为权重矩阵在模型构建时已钉死为 `[VOCAB, HIDDEN]`。读源码时这种「自我定位声明」能帮你判断哪些文件值得精读、哪些只需知道入口函数。

### 4.3 调度与执行层文件族

#### 4.3.1 概念说明

调度与执行层回答两个问题：**这一步喂哪些行**（调度），**怎么把一步算完**（执行）。它涉及三种线程与一条跨线程指令协议：

| 线程 | 名字 | 数量 | 职责 |
| --- | --- | --- | --- |
| 引擎线程 | `glm52-engine-{rank}` | 每逻辑 DP rank 1 个 | `scheduler/mod.rs` 的 `Glm52Engine::serve_loop`：intake、admission、规划、发指令 |
| worker 线程 | `glm52-rank-{rank}` | 每 GPU 1 个（TP4 时 1 引擎驱动 4 镜像 worker） | `runner.rs` 的 `Glm52RankWorker`：独占 CUDA 上下文，执行指令 |
| tokio runtime | — | 进程级 | frontend 请求路由、KV store 的 resolver 任务 |

线程间通信用 crossbeam 通道上的 `Glm52RankCommand` 指令（12 个变体，见 4.3.3）。文件分工：

| 文件 | 职责 |
| --- | --- |
| `model_line.rs` | `Glm52Line`：`ModelLine` 的 `probe`/`validate`/`serve_plan`/`launch` 四段实现 + `Glm52Cli`（u1-l2 已讲） |
| `lib.rs` | `Glm52LaunchOptions`/`Glm52MoeTopo`/`Glm52Drafter` 类型 + `launch()` 入口校验 + `start_engine()` 六阶段总装（u1-l4 详解） |
| `weights.rs` + `weights/` | checkpoint 索引解析（`Glm52WeightManifest`）、按拓扑切分 `all_rank_load_bundles`、mmap/异步/可选 staging 加载（u2 单元详解） |
| `rendezvous.rs` | 多进程 EP fleet 的一次性 TCP 握手：rank 0 进程生成 NCCL unique id 并分发给其他进程，此后再无跨进程控制面（u8-l2 详解） |
| `runner.rs` | worker 线程与 `Glm52RankCommand` 协议；`Glm52StepFlags`（consume/lease/eager）等跨线程消息类型 |
| `scheduler/mod.rs` | `Glm52EngineSpec`（引擎的输入）与 `Glm52Engine`（引擎本体）：`run → bootstrap → serve_loop → teardown` |
| `scheduler/admission.rs` | `validate_request` 门口快速拒绝 + `admit_from_queue` 全生命周期 KV 预算下填 slot |
| `scheduler/plan.rs` | 纯函数规划：`plan_step_shape` 选 bucket、`plan_prefill_spans` 切 span、`lease_flags`、`collect_sampling_rows` |
| `scheduler/slot.rs` | `Glm52SlotState`：每 slot 纯数据状态机（`feed_want`/`advance_span`） |
| `scheduler/graph.rs` | `precapture_step_graphs`（预捕获即 fleet 会合）与 graph PNG 导出 |
| `scheduler/load.rs` | 经 watch 通道向 frontend 发布 `SchedulerMetrics` |
| `scheduler/offload.rs` | P/D 分离信封 + 零义务 resolver（u9-l4 详解） |
| `scheduler/mtp.rs` | 一个 rank 的 native MTP 草稿轮 |
| `model/mod.rs` | `Glm52RankModel`：非专家全量路径 + 本地专家；`GLM52_MAX_STEP_ROWS=96`、`GLM52_DECODE_BUCKETS`、`Glm52StepShape`/`Glm52StepKv` |
| `model/build.rs` | 从驻留张量提取一层权重（MLA + indexer + dense/MoE + norm） |
| `model/step_body.rs` | `run_step_body`：被 CUDA Graph 捕获的整步 forward 主体（embed → 78 层 → lm_head → argmax） |
| `model/launch_ahead.rs` | 投机回放状态机：当前步执行期间提前入队下一步 graph（u6-l4 详解） |
| `model/mtp.rs` | native MTP 服务化驻留（草稿轮的 KV 与图） |

#### 4.3.2 核心流程

引擎主循环 `serve_loop` 一圈的骨架（简化伪代码）：

```
loop {
    intake:   非阻塞 try_recv 收请求（EP 引擎永不阻塞等待——空闲也要全速跑 padding 步；
              镜像引擎是唯一的 collective 发起者，全空闲时才允许阻塞）
    drain:    收割 offload resolver 完成的入列
    admit:    admission 冻结于投机未回收时（lease 钉住了下一步形状）
    若 prefill_only: prefill_step（TP4 分块路径）
    否则:           submit_and_join_step（EP 路径）
        ├─ plan:    feed_wants 快照 → plan_step_shape 选最小覆盖 bucket
        ├─ command: Glm52RankCommand::Step { inputs, shape, kv, flags, sampling, seed }
        ├─ worker:  线程收指令 → decode_step →（命中回放图则 cuGraphLaunch，
        │           否则）run_step_body → 返回每行下一 token
        └─ apply:   apply_step_outputs 推进 slot 状态机、产出 TokenEvent
    (可选) run_draft_round: DSpark / native MTP 草稿轮
}
```

启动期顺序（u1-l4 将逐步展开）：`validate_startup` → 各 worker `LoadWeights` → `BuildFixed`（两阶段构建的第一阶段，回报空闲 VRAM）→ 由 fleet 最小空闲 VRAM 定 `max_model_len` → `FinishKv`（第二阶段，分配池）→ `SetupComm`（集合通信初始化，唯一需要全 rank 同步的一步）→ spawn 引擎线程 → 引擎 `bootstrap` 预捕获全部 bucket 的 CUDA Graph。

#### 4.3.3 源码精读

[runner.rs:L205-L312](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L205-L312)
跨线程指令协议 `Glm52RankCommand` 的全部 12 个变体。逐个看注释能读出架构事实：`Step` 的注释写明「引擎每步无条件发送本指令——空闲 rank 以 padding 行进场、输出被丢弃、bucket 是 rank 本地的」；`MtpDraft` 写明「每个 EP rank 的引擎每步发一条，包括没有活提案的 rank（padding 行进固定链）——不存在跨 rank 的轮类协商」；`DumpDecodeGraph` 写明「图句柄绝不跨线程/上下文所有权边界」。

[runner.rs:L319-L341](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L319-L341)
`Glm52RankWorker::spawn`：为每个 placement 起一个 `glm52-rank-{rank}` 线程，线程内第一时间创建该设备的 CUDA 上下文（`Glm52RankGpuContext::new`），失败经 startup 通道回报。CUDA 上下文与线程绑定，就是所有 GPU 工作都必须经指令协议发给 worker 的原因。

[runner.rs:L563-L567](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L563-L567)
类型别名 `Glm52Worker = Glm52RankWorker`，注释交代了历史与设计：引擎只驱动**本地** worker；跨节点 fleet 是每节点一个进程加入同一 DeepEP communicator，曾经的 TCP rank-host 已退役。

[scheduler/mod.rs:L195-L223](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L195-L223)
`Glm52EngineSpec`：spawn 引擎前的全部输入——自己的 worker 列表（EP 恰好 1 个，TP4 镜像全部）、本 rank 的 `BlockPool`、进程级 `KvStore`、drafter、`load_tx` 指标通道、`startup_tx` 启动报告通道。这个结构体就是「引擎拥有什么」的清单。

[scheduler/mod.rs:L380-L389](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L380-L389)
引擎线程的生命周期四段：`bootstrap`（预捕获图 + 可选导出）→ 报告成功 → `serve_loop` → `teardown`。任何一段失败都先经 `startup_tx` 回报再收线程。

[scheduler/mod.rs:L391-L417](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L391-L417)
`bootstrap` 的关键句：「每个引擎捕获**相同的固定 bucket 序列**，所以捕获期内的集合通信无需任何协调者就在 fleet 间配对——**预捕获序列本身就是 bootstrap 会合**」。这是 free-running 设计（u7-l1）的入口事实。

[scheduler/mod.rs:L419-L460](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/mod.rs#L419-L460)
`serve_loop` 开头：EP 引擎的 intake 用 `try_recv` 非阻塞收请求（注释解释了为什么镜像引擎例外）；resolver 收件箱只装「调度就绪」的请求；admission 在有投机 lease 未回收时冻结。

[model/mod.rs:L288-L298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L288-L298)
执行层的两个核心常量：`GLM52_MAX_STEP_ROWS = 96`（96 的来由——min-gemv token 切分、MQA AOT 批量守卫、decode-feed kernel 都恰好扩到这里）与 `GLM52_DECODE_BUCKETS = [1,2,4,8,16,32,48,64,96]`（每个 bucket 有自己的捕获图、scratch arena 与 FlashMLA 计划；离散 bucket 而非连续值是为了保住整步图的固定形状契约）。

[model/mod.rs:L321-L344](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L321-L344)
`Glm52StepShape`：一步的形状描述——bucket 行数、每行对应的 slot（一个 slot 可占多行，即 span）、`active_rows` 显式区分真实行与 padding 行（因为单 token 提示 `[0]` 与 padding 行在数值上不可区分）。这类「自描述步形状」结构是调度层与执行层之间的全部接口。

[model_line.rs:L229-L234](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L229-L234)
`Glm52Line::launch`——`ModelLine` 分发链的最后一段，从 `LaunchContext` 组装 `Glm52LaunchOptions` 后调 `crate::launch`。u1-l4 将沿这条线走完全部启动阶段。

[rendezvous.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rendezvous.rs#L1-L13)
跨进程唯一的「控制面」：一次性 TCP 握手分发 NCCL unique id；之后各进程的引擎再不互发任何消息，同步全靠集合通信的背压。文件虽小，但它划定了「多进程部署时哪些代码在本进程、哪些不在」。

#### 4.3.4 代码实践：给 12 条指令分类并跟踪一条

1. **实践目标**：把 `Glm52RankCommand` 的 12 个变体分成三类——启动期指令、每步指令、辅助指令——并跟踪「每步指令」从产生到消费的两端。
2. **操作步骤**：
   - 读 [runner.rs:L205-L312](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L205-L312)，每个变体的文档注释开头都会标注 `Non-collective` / `Collective` / 其他信息，据此分类。
   - 再搜索 `Glm52RankCommand::Step` 在 `src/` 中的出现位置（`rg -n 'Command::Step' src/`），找到发送端（`scheduler/mod.rs` 的 `submit_and_join_step` 附近）与接收端（`runner.rs` 的命令 match）。
3. **需要观察的现象**：
   - 启动期：`LoadWeights`、`BuildFixed`、`FinishKv`、`SetupComm`（标注 Collective）、`LoadDspark`、`FreeVram`；
   - 每步：`Step`、`MtpDraft`（标注 Collective）、`Draft`、`PrefillChunk`；
   - 辅助：`DumpDecodeGraph`、`Shutdown`。
   - 发送端在引擎线程、接收端在 worker 线程；`Step` 的响应通道回传 `[u32; GLM52_MAX_STEP_ROWS]`（每行下一 token）。
4. **预期结果**：12 个变体各归其类；两条标注 `Collective` 的指令（`SetupComm`/`MtpDraft`）恰好对应「启动期唯一全 rank 同步点」与「每步固定链的 MTP 轮」。`rg` 只读可直接执行；分类答案本讲已给出，重点是你自己从注释里读出来。
5. 若想确认 `Step` 响应类型的形状，看 [runner.rs:L252-L263](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L252-L263) 即可，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：引擎线程与 worker 线程为什么必须是两个线程？能不能合并成一个？

**答案**：worker 线程独占 CUDA 上下文（[runner.rs:L332-L341](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L332-L341) 线程一启动就 `Glm52RankGpuContext::new`），而引擎线程要做纯 CPU 的规划、admission、指标发布。TP4 镜像拓扑下一个引擎要驱动 4 个 worker 发同一步，且引擎线程数量（每逻辑 rank 1 个）与 GPU 数解耦。合并会让「图句柄跨上下文」类错误成为可能——`DumpDecodeGraph` 的注释明确把图留在 worker 上就是这个原因。

**练习 2**：`Glm52EngineSpec` 里为什么既有 `pool: Arc<BlockPool>` 又有 `store: Arc<KvStore>`？

**答案**：`pool` 是本 rank 的逻辑 KV 池（64-token 内容哈希页），引擎每步直接用它做页表决策；`store` 是进程级共享的 KV 存储（offload/P-D 的 resolve/seal/retire 都走它），在引擎 spawn 之前就已构建（`spec` 注释说明 store 的 rank 表在构建期冻结）。二者分层对应「本 rank 的调度事实」与「跨组件的存储编排」，u7-l5/u8-l3 分别展开。

**练习 3**：`scheduler/load.rs` 只有 38 行，凭什么独立成文件？

**答案**：职责单一——「rank 本地调度负载快照导出给 frontend」（[load.rs:L1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/load.rs#L1)）。本 crate 的文件拆分遵循「一个文件一个可命名职责」而非按行数，同类例子还有 `moe_ep8.rs`（18 行）与 `scheduler/mtp.rs`（78 行）。

### 4.4 测试与基准模块：为何与生产代码同树

#### 4.4.1 概念说明

本 crate 的测试不放在独立的 `tests/` 集成测试目录，而是以 `#[cfg(test)]` 模块与生产代码**同文件树**编译。三个理由都能在源码里找到证据：

1. **oracle 门直接调用生产函数**：`oracle/` 下的精度门重放种子输入并断言落在硬编码探针常量的 RMS 容差内——被测对象就是 `mla_decode`、`layer`、`indexer` 这些 `pub(crate)` 内部函数，独立集成测试目录根本看不到它们。
2. **契约测试需要内部状态**：`scheduler/contract_tests.rs` 在 CPU 上离线复刻引擎的 KV 契约（调度失败在服务中是引擎级致命错误，所以全生命周期预留必须在这里证明），依赖 `testkit.rs` 提供的桩件与 `slot.rs` 的测试探针。
3. **有些探针只能埋在生产路径里**：`freerun_probe.rs` 是 `#[cfg(test)]` 的模块，但它的记录钩子挂在 `runner::step` 与 `scheduler::mtp::run_mtp_round` 的**生产代码**里——因为 free-running 门要探测的就是生产步路径本身，不是孤立 kernel。

此外每个文件的行内 `mod tests` 承担纯逻辑单测（如 `lib.rs` 的 `topology_tests`、`max_model_len_tests`），`benches/` 下的 Python `kernel_lab` 则是独立的 kernel 检查/基准流水线（u10-l3 详解），不在 `src/` 内。

#### 4.4.2 核心流程

oracle 门的工作方式（u10-l1 详解，这里只建立地图印象）：

```
Python harness（tools/accuracy/glm52_oracle.py，钉住官方 transformers 建模代码）
  → 用种子输入跑一遍参考实现 → 输出探针常量
  → 常量手工粘贴进 oracle/*.rs
  → Rust 侧把同一份种子输入喂给生产 forward
  → 断言输出落在探针的 RMS 容差内（全部 #[ignore]：需要 GPU + checkpoint）
```

#### 4.4.3 源码精读

[oracle/mod.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mod.rs#L1-L13)
oracle 家族的门面与运行方式：每个门重放 `tools/accuracy/glm52_oracle.py` 生成的种子输入、断言落在硬编码探针常量的 RMS 容差内；全部 `#[ignore]`（需要 GPU + checkpoint）。文档里直接给出单门运行命令模板。

[oracle/mod.rs:L19-L26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mod.rs#L19-L26)
8 个门模块与算子层一一对应：`bookend`、`mla`、`indexer`、`layer`、`layer_ep4`、`mtp_production` 是积木级门；`freerun_ep4`、`freerun_step` 是架构级门（free-running 设计的 go/no-go 证据，`docs/models/glm52/free-running-dp.md` §8）。

[oracle/mla.rs:L1-L14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mla.rs#L1-L14)
单门的样本：种子输入用 splitmix64（整数-only，跨语言比特一致）；浮点探针走容差而非哈希（fp8 GEMM + 吸收注意力的累加顺序不可能与 HF 逐位相等）；输入摘要先于 kernel 断言，使 PRNG 漂移与 kernel bug 分开失败。这套「探针门纪律」在 u10-l1 展开。

[freerun_probe.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/freerun_probe.rs#L1-L9)
「探针埋进生产路径」的样本：gate 4 需要每步每 rank 的路由输出字节、gate 5 需要每轮的空转与耗时，二者都从**服务路径本身**记录（钩子挂在 `runner::step` 和 `scheduler::mtp::run_mtp_round` 里、由 `enabled()` 开关控制），门在两阶段之间抽取记录。

[scheduler/contract_tests.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scheduler/contract_tests.rs#L1-L4)
调度契约测试的定位：离线复刻引擎的「引擎致命 KV 契约」——把 submit 走走的调度/应用序列端到端跑在真实 `BlockPool` 上，不依赖 GPU（u10-l2 详解）。

[lib.rs:L310-L311](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L310-L311) 与 [lib.rs:L1789-L1790](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1789-L1790)
行内测试模块的例子：`topology_tests`（u1-l2 提过的 18 个纯 CPU 组合校验单测）与 `max_model_len_tests`。行内 `mod tests` 放「与被测函数强绑定的纯逻辑」，目录级 `#[cfg(test)] mod` 放「自成体系的测试家族」。

[benches/README.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/benches/README.md)
`src/` 之外的第五块地图：`benches/kernel_lab/` 是 Python 的 fp8 GEMM 检查/基准/对比流水线（ctypes 直开与生产 SASS 同源链接的 so）。它与 `src/oracle/` 的分工：oracle 管「数值对不对」，kernel_lab 管「跑多快、比基线如何」。

#### 4.4.4 代码实践：盘点被 `#[ignore]` 的门

1. **实践目标**：直观看到「哪些测试默认不跑、为什么」。
2. **操作步骤**：
   ```bash
   rg -n '#\[ignore' src/ | head -30
   ```
3. **需要观察的现象**：每一条命中都带理由字符串，例如 `#[ignore = "requires H200 + GLM-5.2-FP8 checkpoint"]`、`#[ignore = "requires 4 GPUs + GLM-5.2-FP8 checkpoint + NCCL >= 2.30.4"]`、`#[ignore = "requires a GPU"]`。
4. **预期结果**：命中集中在 `oracle/` 与各文件的 GPU 冒烟测试（`fp8.rs`、`moe_tp.rs`、`prefill_tp.rs`、`dspark_smoke.rs`、`indexer_smoke.rs`）；反过来说明**不带 `#[ignore]` 的测试都不需要 GPU**——这就是 CLAUDE.md 说 `cargo test --release --workspace --lib` 能在常规环境跑的原因。命令只读，可直接执行。
5. 若你的机器有 CUDA 工具链与 checkpoint，可再运行 `cargo test --release -p pegainfer-glm52 --features glm52 --lib -- --list` 对照测试清单；否则此步「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把这些精度门写成 `tests/` 目录下的集成测试？

**答案**：集成测试只能看到 crate 的**公共** API，而本 crate 的算子全是 `pub(crate)`；更关键的是 EP 门要按 rank 分工运行（rank 0 走完整层、ranks 1..3 各持本地专家重放 collective），这种多 rank 编排需要访问内部状态与 worker 原语，只能在同 crate 的 `#[cfg(test)]` 模块里实现。

**练习 2**：`freerun_probe.rs` 是测试模块，但它的钩子代码在生产文件里。这样的测试可靠吗？

**答案**：可靠且刻意如此——free-running 门验证的命题是「生产步路径的 padding 字节逐 constancy」「固定 MTP 链每轮都在每个 rank 上跑」，这些只能从服务路径本身观测；钩子由 `enabled()` 开关（[freerun_probe.rs:L15-L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/freerun_probe.rs#L15-L16) 附近的静态原子开关）控制在生产构建中零开销关闭。代价是生产文件里多了几行测试钩子——这是「可观测性优先于文件纯净度」的取舍。

**练习 3**：`indexer_smoke.rs` 与 `oracle/indexer.rs` 都测 indexer，差别是什么？

**答案**：见 [indexer_smoke.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer_smoke.rs#L1-L2) 的自我定位：「验证 kernel 启动与输出形状，不是正确性（那需要 oracle 门 + checkpoint）」。smoke = 「能跑通」，oracle = 「数值对」；分层测试让无 checkpoint 的机器也能跑通一半。

## 5. 综合实践

**任务：建立你自己的「功能 → 文件」索引卡。**

下面 8 个功能名词都来自后续讲义。请对每一个：用 `rg -n '<关键词>' src/`（或直接按本讲的族划分猜测再验证）找到对应文件，抄下文件名与一个关键函数/结构体名，并注明它属于哪一层（算子/执行/调度/测试）。完成后对照答案表：

| 功能 | 答案（文件 :: 关键符号 :: 层） |
| --- | --- |
| 前缀缓存的开/关策略 | `scheduler/mod.rs :: prefix_cache_enabled :: 调度`（DSpark 永禁、native MTP 跟随开关） |
| CUDA Graph 预捕获 | `scheduler/graph.rs :: precapture_step_graphs :: 调度`（引擎 bootstrap 期调用） |
| launch-ahead 投机回放 | `model/launch_ahead.rs :: Glm52SpeculatedStep :: 执行`（配合 `plan.rs :: lease_flags`） |
| EP 宽度泛化 | `moe_ep.rs :: 泛型于 shim 实例化 :: 算子`（新宽度 = 新实例化，非新代码） |
| 跨进程 bootstrap 握手 | `rendezvous.rs :: 一次性 TCP 握手 :: 门面/启动` |
| slot 状态机 | `scheduler/slot.rs :: Glm52SlotState :: 调度`（纯数据转换） |
| 上下文上限的 VRAM 推导 | `lib.rs :: derive_max_model_len（L743 起）:: 启动`（u2-l4 详解） |
| 非贪心采样行的收集 | `scheduler/plan.rs :: collect_sampling_rows :: 调度`（配合 `runner.rs :: Glm52RowSample`） |

**验证方式**：8 项中每一项都应该能用一条 `rg` 命令在 1 分钟内复现（例如 `rg -n 'prefix_cache_enabled' src/` 命中 `scheduler/mod.rs`）。若某项搜索结果与答案表不符，说明代码已演进——以你的搜索结果为准，并顺手更新你的索引卡。这套「猜 → 搜 → 验证」的循环，就是后续所有讲义希望你保持的阅读习惯。

## 6. 本讲小结

- `src/` 共 54 个 `.rs` 文件（约 2.7 万行）：`lib.rs` 声明 27 个模块（4 个 `#[cfg(test)]`），`model/`、`scheduler/`、`weights/`、`oracle/` 四个目录子树各司其职。
- 三层落地为文件族：**算子层**（`fp8`/`bookend`/`mla_*`/`indexer`/`layer`/`moe_*`/`prefill_tp`）是不感知调度的 forward 积木，形状全部来自 `config.rs` 常量；**执行层**（`model/` 五件套）把积木组装成可被 CUDA Graph 捕获的整步 forward；**调度层**（`runner.rs` + `scheduler/` 十件套）决定每步喂哪些行。
- 引擎线程（每逻辑 rank 1 个，`glm52-engine-N`）经 crossbeam 通道上的 12 变体指令 `Glm52RankCommand` 驱动 worker 线程（每 GPU 1 个，`glm52-rank-N`，独占 CUDA 上下文）；`Step` 指令每步无条件发送——空闲 rank 以 padding 行进场。
- 测试与生产同树是三个硬理由：oracle 门直接调 `pub(crate)` 生产函数、契约测试需要内部状态、free-running 探针只能埋在生产路径里；需要 GPU/checkpoint 的测试全部带理由字符串 `#[ignore = ...]`。
- 记地图的钥匙是「请求怎么流」：frontend → rank 队列 → `serve_loop`（admission/plan/slot）→ `Step` 指令 → `run_step_body` → `layer` → 各算子 → `TokenEvent`。

## 7. 下一步学习建议

- 下一讲 **u1-l4（启动链路）**将沿本讲地图走完 `Glm52Line::launch → validate_startup → start_engine 六阶段 → EngineHandle` 的完整时序，把你在本讲看到的 `LoadWeights/BuildFixed/FinishKv/SetupComm` 指令序列串成一条时间线。
- 进入 u2 之前，建议先自己精读一遍 [src/config.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L1-L30)（305 行，纯常量与校验），它是所有算子形状的源头，也是本讲反复提到的「几何钉死在编译期」的具体形态。
- 若想先建立全局设计观，可搭配阅读 `docs/models/glm52/free-running-dp.md`（u7-l1 的主参考）——本讲 4.3 里「引擎永不静默等待」「预捕获即会合」等断言的完整论证都在那里。
