# 一条真实模型线：Qwen3Line 的独占 CLI 与校验矩阵

> 前置讲义：u2-l1（ModelLine trait 的接缝）、u2-l2（SharedArgs 三层 flag 体系）、u2-l3（注册表自检）。
> 本讲把前几讲的抽象骨架落到一条真实的、默认编译进二进制的模型线上。

## 1. 本讲目标

学完本讲，你应该能够：

1. 分组列出 Qwen3Line 的全部独占 CLI flag：LoRA（`--enable-lora` / `--lora-modules` / `--max-loras` / `--max-lora-rank`）、KV P2P/PD（`--kv-p2p-flush-on-finish` / `--kv-pd-vllm-seed` / `--kv-pd-vllm-namespace` / `--kv-pd-miss-wait-ms`）、显存预算（`--gpu-memory-utilization` / `--kv-cache-memory-margin-mib` / `--kv-page-size`）、`--batch-invariant`、`--eagle3-draft-model-path`。
2. 读懂 `validate()` 冲突矩阵的写法：它以「`provided` 出身集合 + 两面 CLI 的当前值」交叉判断，违反即返回 `CliError::rule`，且全部发生在引擎加载之前。
3. 跟踪 `launch()` 如何把 `SharedArgs` 与 `Qwen3Cli` 折叠成一个 `Qwen3LaunchOptions`，交给 crate 内部的 `crate::launch`，最后包成 `LaunchedEngine::Stepped` 交还 server。

## 2. 前置知识

本讲默认你已从 u2-l1～u2-l3 建立了以下认知，这里只做一句话回顾：

- **ModelLine trait**：模型线暴露给 server 的全部表面，8 个方法里 `name` / `probe` / `launch` 必选，其余五个有默认实现（u2-l1）。
- **三层 flag 体系**：核心 flag（`CORE_ARGS`，全线放行）、共享 flag（`SharedArgs` 定义，消费权由 `consumed_shared_args` 声明）、线独占 flag（`augment_cli` 挂上）（u2-l2）。
- **provided 出身语义**：`provided_args` 借 clap 的 `ValueSource` 记录「用户显式提供过哪些 flag」，与默认值无关——因为默认值永远存在，只能问出身，不能比值（u2-l2）。
- **注册表自检**：`ModelLineRegistry::new` 在构造期对 flag id 与拼写查唯一性，并校验 `arg_requirements` 引用的目标 id 真实存在（u2-l3）。

再补几个本讲会遇到的领域术语（初学者向）：

| 术语 | 通俗解释 |
|---|---|
| LoRA | Low-Rank Adaptation：挂在基座模型上的小权重「补丁」（适配器），运行时可在线切换，比整模型小几个数量级。服务 LoRA 意味着每步可能重指向不同适配器。 |
| P/D 分离 | Prefill/Decode 分离部署：处理 prompt 的 prefill 实例与逐 token 生成的 decode 实例分开部署，中间靠转移 KV（键值缓存）衔接。 |
| KV P2P | 跨实例的 KV 点对点共享：块哈希注册到 MetaServer，缺失前缀经 RDMA 从同伴实例拉回。 |
| 投机解码 | Drafter（小草稿模型）先猜若干 token，Target（大模型）一次验证，命中即省多步前向。Qwen3 支持 DFlash 与 EAGLE-3 两种 drafter。 |
| batch-invariant | 数值路径与 batch 组成无关：同一条 prompt 无论和谁同批，输出逐位相同。要求每个 prompt 的 prefill 分块走自己的网格。 |
| PYTHONHASHSEED | vLLM 前缀缓存哈希依赖的 Python 随机化种子；P/D 两侧必须共享同一值才能对上哈希。 |

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `../pegainfer-qwen3/src/model_line.rs` | **本讲主角**。`Qwen3Line` 的全部实现：`Qwen3Cli` 独占 flag、`validate` 冲突矩阵、`launch` 选项组装，以及一组无 GPU 即可运行的单元测试。 |
| `../pegainfer-frontend/src/model_line.rs` | 契约侧。`ModelLine` trait、`SharedArgs`、`LaunchContext`、`CliError`、`ArgRequirement`、`CliDecodeOverlap` 的定义。 |
| `../pegainfer-qwen3/src/lib.rs` | `Qwen3LaunchOptions` 定义与 `crate::launch`：真正持有 Qwen3 启动策略的地方。 |
| `../pegainfer-qwen3/src/weights.rs` | `Qwen3MemoryOptions`（显存预算三参数）及其 `validate()`。 |
| `../pegainfer-qwen3/src/green_ctx.rs` | crate 内部 `DecodeOverlap` 枚举（CLI 枚举的引擎侧对应物）。 |
| `../pegainfer-qwen3/src/config.rs` | `probe_config_json`：probe 的第二级身份检查。 |
| `../pegainfer-frontend/src/vllm/lora.rs` | `parse_lora_modules_arg`：`--lora-modules` 的取值解析器。 |
| `src/main.rs` | server 侧调用点：校验链的顺序与 `spawn_blocking` 加载引擎。 |

永久链接均指向当前 HEAD（`af9e40e1`），路径相对 `pegainfer-server/` 书写。

## 4. 核心概念与源码讲解

### 4.1 Qwen3Cli：Qwen3 独占的 CLI 面

#### 4.1.1 概念说明

u2-l2 讲过：共享 flag「定义一次、消费权按线声明」。但每条模型线总有只属于自己的旋钮——Qwen3 的 LoRA 服务、KV P2P/PD 角色、显存预算、数值不变式、EAGLE-3 投机解码，别的线没有这些概念。这些 flag 定义在 qwen3 crate 私有的 `Qwen3Cli` 结构体里，server 二进制**看不见它的类型**，只能在 `--help` 里看见它的拼写。这就是「独占」的含义：类型知识不越过接缝，拼写知识对用户公开。

除了独占 flag，本模块还一并看 Qwen3Line 对外的两份「CLI 声明」：

- `consumed_shared_args`：声明这条线读哪些共享 flag——15 个，是共享面上绝大多数；
- `arg_requirements`：声明独占 flag 的无条件依赖边，交给注册表在 clap 解析期执行。

#### 4.1.2 核心流程

从 flag 定义到 server 命令行的装配过程：

1. server 构造命令时，`Qwen3Line::augment_cli` 把 `Qwen3Cli` 的字段并入 clap 命令（`Qwen3Cli::augment_args`，clap derive 的机制）。
2. clap 解析合并后的命令：独占 flag 的 `value_parser`（如 `parse_max_lora_rank_arg`）与 `arg_requirements` 声明的依赖都在**这一步**执行——早于读取 `config.json`，更早于引擎加载。
3. 检测命中 Qwen3 后，`validate` / `serve_plan` / `launch` 需要读回独占值时，经帮手函数 `cli(ctx)` 用 `Qwen3Cli::from_arg_matches(ctx.matches)` 从合并命令的完整解析结果里取回——因为命令已被同一套定义解析过，这里只可能因注册表 bug 而 panic，不会因用户输入失败。

```text
augment_cli(Qwen3Cli 字段) ──► 合并命令 ──► clap 解析（值门 + 依赖门）
                                          │
                                          ▼
                        cli(ctx) = Qwen3Cli::from_arg_matches(matches)
                        （validate / serve_plan / launch 各自调用，取得类型化的独占值）
```

#### 4.1.3 源码精读

模块文档一句话点明本文件的全貌：config 探测、Qwen3 独占 CLI 段、跨 flag 规则、launch 选项组装，server 只见到 `&MODEL_LINE`（[../pegainfer-qwen3/src/model_line.rs:1-3](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L1-L3)）。

**独占 flag 全表**。`Qwen3Cli` 是一个 clap derive 结构体（[../pegainfer-qwen3/src/model_line.rs:35-116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L35-L116)），按功能分五组：

| 分组 | flag | 类型 / 默认 | 用途 |
|---|---|---|---|
| LoRA | `--enable-lora` | `bool`，`false` | 开启 Qwen3 LoRA 服务模式（[L38-L40](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L38-L40)） |
| LoRA | `--lora-modules` | `Vec<LoraModule>`，可重复 | 启动时装载的适配器；接受 `name=path`、JSON 对象或单元素 JSON 列表（[L42-L45](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L42-L45)） |
| LoRA | `--max-loras` | `usize`，默认 `1`（`Qwen3LoraOptions::DEFAULT_MAX_LORAS`） | 常驻适配器数量上限（[L47-L49](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L47-L49)） |
| LoRA | `--max-lora-rank` | `usize`，默认 `64`；只接受 `{1,8,16,32,64,128,256,320,512}` | 支持的最大 LoRA rank（[L51-L53](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L51-L53)；取值集合见 [../pegainfer-qwen3/src/lib.rs:52-54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L52-L54)） |
| KV P2P/PD | `--kv-p2p-flush-on-finish` | `bool`，`false` | P/D **prefill 角色**：请求终局前屏障 KV 落盘与注册，让 HTTP 响应本身成为「KV 就绪」信号（[L55-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L55-L60)） |
| KV P2P/PD | `--kv-pd-vllm-seed` | `Option<String>`，PYTHONHASHSEED 格式 | P/D **decode 角色**：与 vLLM prefill 对端共享的哈希种子（[L62-L69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L62-L69)） |
| KV P2P/PD | `--kv-pd-vllm-namespace` | `Option<String>`，8 位小写 hex | pegaflow connector 命名空间摘要（[L71-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L71-L77)） |
| KV P2P/PD | `--kv-pd-miss-wait-ms` | `u64`，默认 `5000` | 零命中等待窗口，须低于执行器 15s 远程取数期限（[L79-L84](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L79-L84)） |
| 显存预算 | `--gpu-memory-utilization` | `f64`，默认 `0.90` | 总显存预算比例，KV 池吃掉刨去权重与运行时后的剩余（[L86-L91](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L86-L91)） |
| 显存预算 | `--kv-cache-memory-margin-mib` | `usize`，默认 `150` MiB | 尺寸化之后再留出的碎片余量（[L93-L96](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L93-L96)） |
| 显存预算 | `--kv-page-size` | `usize`，默认 `16`（或 `64`） | KV 页（块）大小，受 FlashInfer 内核约束（[L98-L102](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L98-L102)） |
| 数值不变式 | `--batch-invariant` | `bool`，`false` | 单卡批量不变式服务；要求 `--no-prefix-cache`（[L104-L108](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L104-L108)） |
| 投机解码 | `--eagle3-draft-model-path` | `Option<PathBuf>` | EAGLE-3 drafter 模型路径；贪婪请求独占活跃时起草（[L110-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L110-L115)） |

注意 struct 上方那条**普通注释**（非 doc 注释）：作者特意说明「doc 注释会经 clap 的 augment 覆盖命令的 about」，所以结构级说明用 `//` 而字段的 `///` 进 `--help`（[../pegainfer-qwen3/src/model_line.rs:32-34](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L32-L34)）。`SharedArgs` 上有同款注释（u2-l2 已见）。

**三个取值解析器**把「格式错误」挡在 clap 解析期（比 u2-l2 的「值门」还要早——连 `config.json` 都还没读）：

- `parse_max_lora_rank_arg`：数值必须落在支持集合内，否则报出全部合法值（[../pegainfer-qwen3/src/model_line.rs:370-382](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L370-L382)）；
- `parse_pythonhashseed`：必须是 `[0, 4294967295]` 的十进制整数且**不允许 `+` 前缀**——注释说明了动机：一个格式良好但与对端不相等的种子会派生出永远匹配不上的 key 空间，这类配置错误必须在启动期失败，而不是变成「每个请求都慢」（[../pegainfer-qwen3/src/model_line.rs:384-394](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L384-L394)）；
- `parse_pegaflow_namespace`：恰好 8 个小写 hex 字符（[../pegainfer-qwen3/src/model_line.rs:396-408](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L396-L408)）。

`--lora-modules` 的解析器在 frontend 侧：`parse_lora_modules_arg` 接受 `name=path`、JSON 对象或恰好一个元素的 JSON 列表，多元素列表会被拒绝并提示改用多次 `--lora-modules`（[../pegainfer-frontend/src/vllm/lora.rs:73-96](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/vllm/lora.rs#L73-L96)）。

**两份 CLI 声明**。`consumed_shared_args` 列出 Qwen3 消费的 15 个共享 flag id——从 `cuda_graph` 到 `decode_sm_pct`，几乎整个共享面（[../pegainfer-qwen3/src/model_line.rs:149-167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L149-L167)）。没被消费的共享 flag（如 `dp_size`）若被显式提供，会在 `validate_provided` 的 consume-or-reject 里以 `--dp-size is not used by Qwen3` 拒绝——本文件的单测 `rejects_other_lines_flags` 逐字断言了这条消息（[../pegainfer-qwen3/src/model_line.rs:503-510](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L503-L510)）。

`arg_requirements` 声明四条无条件依赖边（[../pegainfer-qwen3/src/model_line.rs:169-179](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L169-L179)）：

```text
kv_p2p_flush_on_finish ──requires──► kv_p2p_metaserver_addr
kv_pd_vllm_seed        ──requires──► kv_p2p_metaserver_addr, kv_pd_vllm_namespace
kv_pd_vllm_namespace   ──requires──► kv_pd_vllm_seed
kv_pd_miss_wait_ms     ──requires──► kv_pd_vllm_seed
```

为什么要写在这里而不是 clap 属性里？trait 方法的文档说得很直白：让 release 构建也能校验每条被引用的 id（[../pegainfer-frontend/src/model_line.rs:357-361](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L357-L361)）。u2-l3 还讲过：注册表在构造期就核对了这些目标 id 真实存在，所以 launch 里那句 `.expect("clap requires kv_pd_vllm_namespace")`（见 4.3）是安全的。

**probe 顺便一提**（细节在 u1-l4）：两级检查——先看 `model_type == "qwen3"`，再交给 `crate::probe_config_json` 验证 `architectures` 含 `Qwen3ForCausalLM`（[../pegainfer-qwen3/src/model_line.rs:137-143](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L137-L143)；[../pegainfer-qwen3/src/config.rs:895-910](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/config.rs#L895-L910)）。这意味着一份只含 `{"model_type":"qwen3","architectures":["Qwen3ForCausalLM"]}` 的最小 config 就能通过检测门——综合实践会用到这个事实。

#### 4.1.4 代码实践：盘点 `--help` 与三个取值错误

1. **实践目标**：亲眼确认「独占 flag 的拼写公开、类型私有」，并验证取值解析发生在读取 `config.json` 之前。
2. **操作步骤**：
   - 在 `pegainfer-server/` 目录运行 `cargo run --release -- --help`，对照上面的分组表，把属于 Qwen3 独占的 flag 逐一找出来（`--tp-size` 等共享 flag 不在表里）。
   - 依次运行三条非法取值（注意都不需要真实模型目录）：
     ```bash
     cargo run --release -- --max-lora-rank 7
     cargo run --release -- --kv-pd-vllm-seed 0x10
     cargo run --release -- --kv-pd-vllm-namespace ABCDEF12
     ```
   - 对照运行 `cargo run --release -- --model-path /nonexistent`，观察这条的错误是什么。
3. **需要观察的现象**：
   - 前三条的报错来自取值解析器（期望分别是 `--max-lora-rank must be one of: ...`、`PYTHONHASHSEED must be a decimal integer in [0, 4294967295]`、`namespace must be an 8-char lowercase hex digest`），并且**报错时没有任何读 config.json 的日志**；
   - 第四条才会报 `failed to read /nonexistent/config.json`——证明取值门在文件读取之前。
4. **预期结果**：三条取值错误以 clap 的标准错误形态退出（exit code 2，`error: invalid value ...`），第四条是 anyhow 的上下文链。本讲义写作环境无 GPU 与构建产物，以上输出为依据源码的推断，**待本地验证**。
5. 顺带一提：`--kv-pd-vllm-seed 5 --kv-pd-vllm-namespace deadbeef` 单独给不会报错——依赖边只要求「同时提供」，真正的联网检查在引擎启动期。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--lora-modules` 可以在命令行出现多次，而 `--kv-pd-vllm-seed` 不能？
**答案**：`lora_modules` 的字段类型是 `Vec<LoraModule>`，clap 会把多次出现收集成列表；`kv_pd_vllm_seed` 是 `Option<String>`，第二次出现会被 clap 判为重复参数而报错。

**练习 2**：`"+5".parse::<u32>()` 在 Rust 里其实能成功，那 `parse_pythonhashseed` 为什么还要专门拒绝 `+` 前缀？
**答案**：因为种子是与 vLLM 对端逐字共享的字符串——数值上相等的 `"5"` 与 `"+5"` 派生出的哈希 key 空间并不相等，会导致永远匹配不上对端。解析器的文档注释写明：这种「格式良好但永不匹配」的配置错误必须在这里失败，而不是退化成慢请求。

**练习 3**：`--max-lora-rank` 的默认值从哪里来？合法值集合呢？
**答案**：默认值来自 `Qwen3LoraOptions::DEFAULT_MAX_LORA_RANK`（= 64），合法值集合是 `SUPPORTED_MAX_LORA_RANKS = [1, 8, 16, 32, 64, 128, 256, 320, 512]`，两者都定义在 [../pegainfer-qwen3/src/lib.rs:52-54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L52-L54)；单测 `lora_default_rank_is_64` 锁住了默认值（[../pegainfer-qwen3/src/model_line.rs:526-528](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L526-L528)）。

### 4.2 validate：冲突矩阵

#### 4.2.1 概念说明

`validate` 是 ModelLine 的第五个方法（有默认实现的那个「空通过」），Qwen3Line 把它写成了一个大矩阵。它解决的问题是：**有些 flag 组合在语法上完全合法、每个值也合法，但组合起来语义不成立**。例如 `--batch-invariant` 与 `--tp-size 2`：单卡数值不变式的前提在张量并行下根本不存在。

这类规则不能放进 clap 的 `requires`/`conflicts`（那是「存在性依赖」，u2-l2 讲过判据），因为它们要交叉读**值**（`tp_size` 的数值、`decode_overlap` 的档位）与**出身**（`provided` 集合）。所以 trait 文档把它定位为「`arg_requirements` 之外的跨 flag 规则，在注册表的 consume-or-reject 之后运行」（[../pegainfer-frontend/src/model_line.rs:363-371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L363-L371)）。

#### 4.2.2 核心流程

`validate` 在 server 主流程中的位置（u1-l3 讲过整条链，这里只看本讲相关的三行）：

```text
registry.detect(&config)            // 检测门（u1-l4）
registry.validate_provided(...)     // 归属门：consume-or-reject（u2-l3）
shared.validate(&provided)          // 语义门（共享面，u2-l2）
line.validate(&ctx, &provided)      // ◄── 本讲：线内冲突矩阵
line.serve_plan(&ctx)               // 服务计划（u2-l5）
spawn_blocking { line.launch(ctx) } // 引擎加载（4.3）
```

对应 [src/main.rs:177-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L177-L186)。关键时序事实：`info!("=== pegainfer - {} (GPU) ===")` 与 `info!("Loading engine...")` 在 validate **之后**才打印（[src/main.rs:188-189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L188-L189)），所以「日志里有没有出现 Loading engine」就是「错误发生在校验期还是加载期」的可观察分界线。

矩阵的判定输入有三个：

1. `cli(ctx)` 取回的独占值（布尔、Option）；
2. `ctx.shared` 的共享值（数值、档位）；
3. `provided` 出身集合——只在 LoRA 参数组用到一次（`max_loras` / `max_lora_rank` 有默认值，不能比值，只能问「用户是否显式给过」）。

违反任何一条立即返回 `CliError::rule("...")`——`CliError::Rule(String)` 的文档说「规则是散文，不是分类学」，消息本身就是面向用户的解释（[../pegainfer-frontend/src/model_line.rs:51-66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L51-L66)）。

#### 4.2.3 源码精读

`validate` 全体是一个顺序 if 链（[../pegainfer-qwen3/src/model_line.rs:181-286](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L181-L286)），按「触发前提」分成五段：

**第一段：LoRA 参数组的门槛**（[L188-L202](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L188-L202)）。

```rust
if !cli.enable_lora && !cli.lora_modules.is_empty() {
    return Err(CliError::rule("--lora-modules requires --enable-lora"));
}
if !cli.enable_lora
    && (provided.contains("max_loras") || provided.contains("max_lora_rank"))
{
    return Err(CliError::rule(
        "--max-loras and --max-lora-rank require --enable-lora",
    ));
}
```

注意第二条是 `provided` 出身语义的教科书案例：`max_loras` 永远有默认值 `1`，比「值是否等于默认」既不可靠也不诚实，只有「用户显式写过它」才构成「用户想配置 LoRA」的证据。第三条规则方向相反——是共享 flag 拒绝独占 flag：`--dump-graph-png` 与 `--enable-lora` 互斥，消息顺带解释了原因（LoRA 禁用 CUDA Graph）。

**第二段：decode-overlap 的单卡限制**（[L203-L207](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L203-L207)）：`decode_overlap != Off && tp_size > 1` 即拒。注意它读的是共享 flag 的**值**（档位与数值的交叉），这正是它不能进 clap 依赖表的原因。

**第三段：`--batch-invariant` 冲突表**（外层条件 `cli.batch_invariant`，[L208-L242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L208-L242)）。抄成二维表：

| # | 冲突对象（判定条件） | 错误消息（节选） | 消息里的「为什么」 |
|---|---|---|---|
| 1 | `--enable-lora`（`cli.enable_lora`） | `--batch-invariant is not supported with --enable-lora; enable one at a time` | 二者一次只能开一个 |
| 2 | `--decode-overlap` ≠ off | `not compatible with --decode-overlap; the stream override would force the pinned GEMM to bail at runtime` | 流覆盖会迫使钉住的 GEMM 在运行时退坑 |
| 3 | `--kv-offload` | `not supported with --kv-offload: offload keeps prefix matching on ... and a host-tier prefix hit shifts a prompt's chunk boundaries off the request-local grid` | 主机 tier 的前缀命中会把分块边界挪离请求自身网格 |
| 4 | `--dflash-draft-model-path` | `not supported with DFlash speculative decoding; enable one at a time` | 二选一 |
| 5 | `--tp-size > 1` | `not supported with --tp-size > 1; enable one at a time` | 单卡特性 |
| 6 | **缺** `--no-prefix-cache`（`!shared.no_prefix_cache`） | `--batch-invariant requires --no-prefix-cache; prefix-cache hits move a prompt's chunk boundaries off the request-local grid` | 前缀缓存命中同样会挪动分块边界 |

第 6 条是**正向要求**（不是互斥）：batch-invariant 的前提是每个 prompt 的 prefill 在自己的网格上分块，而任何前缀命中都会跳过一段前缀、把边界挪走——所以规则 3 与 6 的「为什么」其实是同一个物理原因的两种表现。

**第四段：`--dflash-draft-model-path` 冲突表**（外层条件 `shared.dflash_draft_model_path.is_some()`，[L243-L259](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L243-L259)）：互斥 `--enable-lora`、`--kv-offload`，要求 `--tp-size=1`。注意 DFlash 是**共享 flag**（`SharedArgs.dflash_draft_model_path`，GLM5.2 也用它接 DSpark drafter，见 [../pegainfer-frontend/src/model_line.rs:198-204](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L198-L204)），而 EAGLE-3 是 Qwen3 独占——同名物在两层体系里各有一个，这是个容易混淆的点。

**第五段：`--eagle3-draft-model-path` 冲突表**——写法换成了「谓词表 + find」（[L260-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L260-L284)）：

```rust
let conflicts = [
    (shared.dflash_draft_model_path.is_some(), "--dflash-draft-model-path"),
    (cli.enable_lora, "--enable-lora"),
    (shared.kv_offload, "--kv-offload"),
    (shared.decode_overlap != CliDecodeOverlap::Off, "--decode-overlap"),
    (cli.batch_invariant, "--batch-invariant"),
];
if let Some((_, flag)) = conflicts.iter().find(|(set, _)| *set) {
    return Err(CliError::rule(format!(
        "--eagle3-draft-model-path is not supported with {flag}"
    )));
}
if shared.tp_size != 1 {
    return Err(CliError::rule(
        "--eagle3-draft-model-path currently requires --tp-size=1",
    ));
}
```

抄成二维表：

| # | 冲突对象（判定条件） | 错误消息 |
|---|---|---|
| 1 | `--dflash-draft-model-path` 已提供 | `--eagle3-draft-model-path is not supported with --dflash-draft-model-path` |
| 2 | `--enable-lora` | `... is not supported with --enable-lora` |
| 3 | `--kv-offload` | `... is not supported with --kv-offload` |
| 4 | `--decode-overlap` ≠ off | `... is not supported with --decode-overlap` |
| 5 | `--batch-invariant` | `... is not supported with --batch-invariant` |
| 6 | `--tp-size ≠ 1` | `--eagle3-draft-model-path currently requires --tp-size=1` |

两段风格对比是有意的教学素材：if 链每条可以有定制的长解释（batch-invariant 的第 2、3、6 条都带物理原因），表 + find 则把同构规则压成数据、按声明顺序取**第一个**命中——所以同时给 `--enable-lora --kv-offload` 时，报的是表里靠前的 `--enable-lora`。

另一个值得注意的不对称：字段文档说 EAGLE-3 「forces the prefix cache off」（引擎侧行为，launch 自己关），而 `--batch-invariant` 要求**用户亲手**传 `--no-prefix-cache`（校验期拒绝）。同样是与前缀缓存不相容，一个是引擎代劳、一个是硬性 CLI 前提。

最后是**纵深防御**：这些规则在引擎深处还会再被查一次。`crate::launch` 里对 TP + decode-overlap 的 `ensure!` 旁边有一条注释：「在 `enable_decode_overlap`（承重墙）也拒绝；在这里先失败，省下整次 TP 模型加载 + 图预捕获」（[../pegainfer-qwen3/src/lib.rs:310-315](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L310-L315)）。同一规则的三层：CLI `validate`（最便宜，连日志横幅都没打）→ `launch` 入口的 `ensure!`（在权重加载前）→ 引擎内部的承重守卫（不信任何调用方）。

#### 4.2.4 代码实践：抄表 + 跑既有单测

1. **实践目标**：把 `--batch-invariant` 的六条规则亲手抄成二维表（不打字抄一遍记不住），并用无 GPU 的单元测试验证矩阵真的在跑。
2. **操作步骤**：
   - 对照 [../pegainfer-qwen3/src/model_line.rs:208-242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L208-L242)，把 4.2.3 里 batch-invariant 表的「错误消息」列遮住，自己凭源码补全，再对照。
   - 运行本文件自带的测试模块（纯 CLI 逻辑，不需要 GPU 与权重）：
     ```bash
     cargo test --release -p pegainfer-qwen3 --lib model_line
     ```
   - 阅读测试帮手 `validate_argv`：它用 `parse_for_line`（u2-l3 讲过的单线注册表帮手）走真实的「解析 → 构造 LaunchContext → validate」路径，config 用空 JSON 占位（[../pegainfer-qwen3/src/model_line.rs:416-427](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L416-L427)）——这也旁证了 validate 完全不读 config 内容。
3. **需要观察的现象**：测试全绿；`eagle3_draft_rejects_unsupported_combinations` 一个用例覆盖了 EAGLE-3 表的 4 行（dflash / lora / kv-offload / tp-size，[L483-L500](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L483-L500)）。
4. **预期结果**：`cargo test` 报告该模块下的用例全部通过（probe 3 个 + graph-png 3 个 + eagle3 1 个 + 其他 4 个左右，以实际输出为准）。注意一个事实：截至本讲写作时，**batch-invariant 的六条规则没有专门的单测**（测试名单里无对应用例）——它们只被源码与人工运行覆盖。这是读者可以动手补测试的地方。
5. 本讲义写作环境未执行构建，运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--max-loras` 的规则用 `provided.contains(...)` 而 `--lora-modules` 的规则用 `!cli.lora_modules.is_empty()`？
**答案**：`lora_modules` 是 `Vec`，默认为空，「非空」本身就说明用户提供了，比值即可；`max_loras` 是 `usize` 且永远有默认值 `1`，值上无法区分「用户特意设了 1」与「没设」，只能查 `provided` 的出身记录。

**练习 2**：`--eagle3-draft-model-path e --enable-lora --kv-offload` 三个一起给，报哪条错？为什么？
**答案**：报 `--eagle3-draft-model-path is not supported with --enable-lora`。谓词表按声明顺序 `find` 第一个命中项，`--enable-lora` 在表中的下标（1）先于 `--kv-offload`（2）。

**练习 3**：batch-invariant 的第 6 条为什么是「requires --no-prefix-cache」而 EAGLE-3 对前缀缓存没有任何 validate 规则？
**答案**：`--batch-invariant` 的正确性依赖「每个 prompt 在自己的网格上分块」，前缀命中会跳过一段前缀、挪动边界，因此必须由用户显式关掉缓存，校验期硬拒；EAGLE-3 与前缀缓存的不相容由引擎侧处理（字段文档写明它「forces the prefix cache off」），无需用户配合，所以不设 CLI 规则。

### 4.3 launch：把两面 CLI 折成 Qwen3LaunchOptions

#### 4.3.1 概念说明

`launch` 是 trait 的最后一个方法，也是唯一必选的「重」方法：加载引擎、启动调度器线程、把句柄交还 server。但本讲聚焦它的**前半段**——选项组装：把 `SharedArgs`（15 个消费字段的值）与 `Qwen3Cli`（13 个独占字段）折叠成 crate 内部的单一结构 `Qwen3LaunchOptions`，再调用 `crate::launch`。

这个折叠有一句设计宣言，写在 `Qwen3LaunchOptions` 的文档注释里：二进制把原始 CLI flag 映射进这个结构；**启动策略（TP→设备映射、LoRA↔CUDA-Graph 互斥等）归 `launch` 所有，跟模型住在一起，不漏进 server**（[../pegainfer-qwen3/src/lib.rs:224-229](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L224-L229)）。也就是说：CLI 层的 `validate` 负责「用户可知的规则」，引擎层的 `crate::launch` 负责「实现可知的策略」——例如 LoRA 会强制关掉 CUDA Graph 并打一条 warn，这发生在 launch 内部而非 validate（[../pegainfer-qwen3/src/lib.rs:275-284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L275-L284)）。

#### 4.3.2 核心流程

`ModelLine::launch`（[../pegainfer-qwen3/src/model_line.rs:296-367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L296-L367)）的折叠伪代码：

```text
launch(ctx):
    cli    = Qwen3Cli::from_arg_matches(ctx.matches)   # 取回独占值
    shared = ctx.shared                                  # 只读共享值

    # 1) offload：三段嵌套组装
    offload = if shared.kv_offload:
        bytes = kv_offload_host_gib 换算成字节
        o = Qwen3OffloadOptions::enabled(bytes)
        o.use_hugepages = kv_offload_hugepages
        if (metaserver_addr, advertise_addr) 都给了:
            o = o.with_p2p(地址 + nics + cli.kv_p2p_flush_on_finish)
        if cli.kv_pd_vllm_seed 给了:
            o = o.with_vllm_compat(seed + namespace.expect(...) + miss_wait)
        o
    else:
        Qwen3OffloadOptions::disabled()

    # 2) lora：enable_lora 才有内容
    lora = cli.enable_lora ? Qwen3LoraOptions{max_loras, max_lora_rank} : None

    # 3) 显存：MiB→字节防溢出，再就地校验取值
    margin_bytes = kv_cache_memory_margin_mib.checked_mul(1<<20) 否则 anyhow 报错
    memory = Qwen3MemoryOptions::new(util, margin_bytes, page).validate()?

    # 4) 一把折成 Qwen3LaunchOptions，调用 crate::launch
    crate::launch(ctx.model_path, Qwen3LaunchOptions{...})    # 返回 Engine
        .map(LaunchedEngine::Stepped)                          # 包成交接类型
```

`crate::launch` 内部再按策略分派：有 LoRA 走 `start_engine_with_lora_control`（铸造控制平面），否则走 `start_engine_with_offload_inner` 并带上 drafter 与图导出路径（[../pegainfer-qwen3/src/lib.rs:337-367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L337-L367)）。

#### 4.3.3 源码精读

**offload 的三段嵌套**（[../pegainfer-qwen3/src/model_line.rs:299-327](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L299-L327)）：GiB→字节换算、开 `Qwen3OffloadOptions::enabled`、贴上 hugepages 开关；两个 P2P 地址**同时**给了才 `with_p2p`（这两个的同时性由 `SHARED_ARG_REQUIREMENTS` 的 `kv_p2p_metaserver_addr → {kv_offload, kv_p2p_advertise_addr, kv_p2p_nics}` 依赖边保证，见 [../pegainfer-frontend/src/model_line.rs:223-232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L223-L232)）；`kv_pd_vllm_seed` 给了才 `with_vllm_compat`。

其中这句最值得停下来看：

```rust
namespace: cli
    .kv_pd_vllm_namespace
    .clone()
    .expect("clap requires kv_pd_vllm_namespace"),
```

一个 `expect` 敢写在这里，是因为它的安全性由**两台前置机器**共同保证：`arg_requirements` 声明了 `kv_pd_vllm_seed → kv_pd_vllm_namespace` 的依赖边（clap 解析期执行），而 u2-l3 讲过注册表在**构造期**校验过这条边的目标 id 真实存在。缺任何一台，这行都是隐患；两台都在，它是把「跨模块不变量」压缩成一行的惯用法。

**lora 与显存预算**（[../pegainfer-qwen3/src/model_line.rs:328-340](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L328-L340)）：`enable_lora.then_some(...)` 把布尔翻成 `Option`；`checked_mul(1 << 20)` 防 MiB→字节换算溢出，溢出时返回 anyhow 错误（这是「深上下文、调用方不分支」错误哲学的又一次体现——u2-l1 讲过 launch 的错误用 anyhow 的原因）。

`Qwen3MemoryOptions::validate()` 只查两件事：`0 < gpu_memory_utilization <= 1`，且页大小在 `{16, 64}` 内（[../pegainfer-qwen3/src/weights.rs:65-78](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/weights.rs#L65-L78)）。注意这两条**没有**对应的 clap 取值解析器——它们住在模型 crate 里，失败的时机在 launch 内部（`spawn_blocking` 线程上），错误链会带上外层 `.with_context(|| format!("failed to start {} engine", line.name()))` 的前缀（[src/main.rs:204-213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L204-L213)）。同一段里 `max_prefill_tokens` 缺省时落到 crate 默认 `DEFAULT_MAX_PREFILL_TOKENS = 1024`（[../pegainfer-qwen3/src/scheduler.rs:416](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/scheduler.rs#L416)）。

**枚举翻译**：CLI 侧的 `CliDecodeOverlap`（frontend，三档：`off`/`stream`/`green-ctx`）经 `resolve_decode_overlap` 翻成引擎侧的 `DecodeOverlap`，green-ctx 档顺带把共享 flag `decode_sm_pct` 塞进变体载荷（[../pegainfer-qwen3/src/model_line.rs:118-126](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L118-L126)；引擎枚举见 [../pegainfer-qwen3/src/green_ctx.rs:29-38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/green_ctx.rs#L29-L38)）。两个同名枚举分居两侧，正是「接缝上只传值、不传类型」的体现。

**最后的交接**：`crate::launch(...).map(LaunchedEngine::Stepped)`（[../pegainfer-qwen3/src/model_line.rs:341-366](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L341-L366)）。Qwen3 已完成向新一代 step 驱动契约的迁移，返回 `Engine`（实现了 `Scheduler`）后包成 `LaunchedEngine::Stepped`——这就是 u1-l3 说过的「LoRA 分支要求 `Stepped`」能对 Qwen3 成立的原因。顺带一提 `serve_plan`（u2-l5 的主角，此处一笔带过）：Qwen3Line 只设 `lora_modules` 一个字段，其余全走 `ServePlan::default()`（[../pegainfer-qwen3/src/model_line.rs:288-294](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L288-L294)）。

**`crate::launch` 持有的策略**（我们只看与 CLI 相关的几条）：TP→设备映射（单卡用 `device_ordinal`，多卡占 `0..tp_size`，[../pegainfer-qwen3/src/lib.rs:270-274](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L270-L274)）；LoRA 强制关 CUDA Graph 并 warn（[L275-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L275-L284)）；图导出的前置检查（[L285-L295](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L285-L295)）；DFlash 与 EAGLE-3 不能同载、drafter 不与 LoRA / decode-overlap 组合（[L316-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/lib.rs#L316-L336)）。这些 `ensure!` 与 CLI `validate` 部分重叠——重叠不是浪费，是「CLI 层早失败省时间、引擎层承重保安全」的分层（见 4.2.3 末尾）。

#### 4.3.4 代码实践：launch 里的廉价检查仍在权重之前

1. **实践目标**：证明「住在 launch 里的取值检查」（显存预算）虽晚于 CLI validate，但仍早于任何权重/GPU 工作。
2. **操作步骤**（复用综合实践准备的 config-only 目录，见第 5 节；也可用本地真实检查点目录）：
   ```bash
   cargo run --release -- --model-path /tmp/qwen3-config-only --gpu-memory-utilization 1.5
   cargo run --release -- --model-path /tmp/qwen3-config-only --kv-page-size 32
   ```
3. **需要观察的现象**：两条命令都**已经打印** `Loading engine...` 横幅（说明已进入 `spawn_blocking` 里的 launch），随后失败；错误链形如 `failed to start Qwen3 engine: gpu_memory_utilization must be in (0, 1], got 1.5` 与 `... page_size must be one of [16, 64], got 32`。目录里没有任何权重分片也足以触发——证明失败点在 `Qwen3MemoryOptions::validate()`（[../pegainfer-qwen3/src/weights.rs:65-78](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/weights.rs#L65-L78)），先于 `crate::launch` 的任何加载动作。
4. **预期结果**：与 4.2 的 validate 失败对比——那次连横幅都没有。两处失败的分界线就是 `info!("Loading engine...")`（[src/main.rs:188-189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L188-L189)）。本讲义写作环境无 GPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`launch` 里 `Qwen3MemoryOptions::new(...).validate()?` 的 `?` 发生在 `crate::launch(...)` 调用之前还是之后？这意味着什么？
**答案**：之前——它写在 `Qwen3LaunchOptions` 结构体字面量内部，而字面量先于 `crate::launch` 求值。意味着取值非法时连 `crate::launch` 都不会进入，CUDA、分词器、权重一概未被触碰，失败成本几乎为零。

**练习 2**：如果把 `kv_pd_vllm_namespace` 从 `arg_requirements` 的依赖边里删掉，4.3.3 里那句 `.expect("clap requires kv_pd_vllm_namespace")` 会变成什么？
**答案**：从一个「被两台机器保证的不变量」退化成真实的崩溃风险：用户只给 `--kv-pd-vllm-seed` 不给 namespace 时，clap 不再拦截，launch 将在 `spawn_blocking` 线程上 panic。这正是 u2-l3 讲的「依赖目标在注册表构造期被校验」的价值所在。

**练习 3**：为什么 `resolve_decode_overlap` 需要同时接收 `overlap` 与 `decode_sm_pct` 两个参数？
**答案**：`decode_sm_pct` 只在 green-ctx 档有意义（u2-l2 讲过它的共享规则），而 CLI 侧 `CliDecodeOverlap::GreenCtx` 变体本身不携带百分比；翻译成引擎侧 `DecodeOverlap::GreenCtx { decode_pct }` 时必须从共享 flag 里把值取来填进变体载荷。

## 5. 综合实践：证明「校验先于 launch」

一个贯穿本讲三个模块的实验：让同一个错误在补齐 flag 后**沿启动流程向前推进**，以此亲测「廉价校验全部排在昂贵的引擎加载之前」。

**准备**：构造一个只含 config、不含权重的模型目录。

```bash
mkdir -p /tmp/qwen3-config-only
# 有本地检查点（models/Qwen3-4B）时：复制完整真实 config
cp models/Qwen3-4B/config.json /tmp/qwen3-config-only/
# 没有检查点时的替代：最小身份 config 也能通过检测门（probe 只查身份，见 4.1.3）
cat > /tmp/qwen3-config-only/config.json <<'EOF'
{"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}
EOF
```

**第一步：抄录两张二维表**。对照源码（[L208-L242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L208-L242) 与 [L260-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L260-L284)），把 4.2.3 的 batch-invariant 表（6 行）与 eagle3 表（6 行）补全「判定条件」与「错误消息」两列——判定条件一列必须写成 Rust 表达式（如 `shared.kv_offload`、`provided.contains("max_loras")`）。

**第二步：运行 A——校验期拒绝**。

```bash
cargo run --release -- --model-path /tmp/qwen3-config-only --batch-invariant
```

预期：终端打印 `--batch-invariant requires --no-prefix-cache; prefix-cache hits move a prompt's chunk boundaries off the request-local grid, so batch-invariant prefill cannot be provided`（消息跨行拼接，见 [L236-L241](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L236-L241)），且**没有** `=== pegainfer - Qwen3 (GPU) ===` 与 `Loading engine...` 日志——错误发生在 [src/main.rs:185](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L185) 的 `line.validate`，早于 L188 的横幅。

**第三步：运行 B——补齐 flag，观察错误推进**。

```bash
cargo run --release -- --model-path /tmp/qwen3-config-only --batch-invariant --no-prefix-cache
```

预期：这次校验全过（batch-invariant 其余五条冲突在默认值下都不触发），横幅与 `Loading engine...` 出现，错误推进到引擎加载阶段：

- 用**最小身份 config** 时，预期失败在 launch 内部对完整 `Qwen3Config` 的解析（serde 缺字段）；
- 用**完整真实 config** 时，预期推进得更远——到分词器或权重分片读取（目录里没有这些文件）。

**第四步：下结论**。把两次运行的「最后一条日志 + 错误首行」填进下表：

| 运行 | 最后日志 | 错误首行（示意） | 失败阶段 |
|---|---|---|---|
| A | （无横幅） | `--batch-invariant requires --no-prefix-cache...` | CLI validate |
| B | `Loading engine...` | 待本地验证 | 引擎加载（config 解析 / 文件读取） |

这就用可观察证据坐实了本讲的核心论断：`validate` 的全部规则都在 `launch` 之前，错误会随 flag 补全而沿装配线前进。本讲义写作环境无 GPU、未执行构建，第二、三步的具体输出**待本地验证**——但「A 无横幅即拒」这一点由 [src/main.rs:185-189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L185-L189) 的语句顺序直接保证。

## 6. 本讲小结

- **独占 CLI 面**：`Qwen3Cli` 五组十三个 flag（LoRA 4、KV P2P/PD 4、显存预算 3、batch-invariant 1、EAGLE-3 1），类型私有、拼写公开；三个取值解析器把格式错误挡在 clap 解析期——早于读取 `config.json`。
- **两份声明**：`consumed_shared_args`（15 个共享 id）划定消费面，`arg_requirements`（4 条边）把存在性依赖交给 clap；后者与注册表构造期校验一起，让 launch 里的 `.expect` 成为安全不变量。
- **冲突矩阵**：`validate` 以「provided 出身 + 两面 CLI 当前值」交叉判定，五段规则覆盖 LoRA 门槛、overlap 单卡限制、batch-invariant（6 条）、DFlash（3+1 条）、EAGLE-3（5+1 条）；两种写法（if 链 vs 谓词表 + find）各有取舍，消息即 API。
- **分层失败**：同一规则可出现在 CLI validate（最便宜）→ `crate::launch` 的 `ensure!`（权重加载前）→ 引擎承重守卫（最深）三层；可观察分界线是 `Loading engine...` 横幅。
- **launch 折叠**：`SharedArgs + Qwen3Cli → Qwen3LaunchOptions → crate::launch → LaunchedEngine::Stepped`；启动策略（TP 设备映射、LoRA 关图、drafter 互斥）归模型 crate 所有，不漏进 server。
- Qwen3 是 trait 的「八方法满配」参照实现（u2-l1 的结论在此落地），且其冲突矩阵中 batch-invariant 一支尚无专门单测——那是读者可贡献的第一块砖。

## 7. 下一步学习建议

- **u2-l5（ServePlan）**：本讲两次路过 `serve_plan` 都一笔带过——下一讲专门讲「引擎加载前前端就必须知道的事实」如何经 `ServePlan` 流出，以及 main.rs 如何按它选 serve 分支。
- **延伸阅读源码**：`../pegainfer-qwen3/src/lib.rs` 的 `crate::launch` 全体（L269-L368）与 `start_engine_with_lora_control` / `start_engine_with_offload_inner` 两个分派终点，看 CLI 选项如何变成引擎线程。
- **对照另一条线**：快速浏览 `../pegainfer-frontend/examples/echo-server.rs` 的 `EchoLine`（三方法最小实现），体会「满配」与「最小」之间的谱系——这也是 u5-l2（从零接入一条新模型线）的预习。
- **单元三预告**：launch 交回的 `LaunchedEngine::Stepped` 里装的是什么？u3-l1 起进入引擎契约：两代契约并存的地图、`Scheduler` trait 与 `drive` 轮询循环。
