# 从消费者看效果：qwen3_kernel_report 如何用一次 profile 调用拿到硬件计数器

## 1. 本讲目标

学完本讲，你应该能够：

1. 默写 `profile_range_with_prepare` 的调用模板——五个要素：context、device_index、range 名、指标列表、prepare/launch 两个闭包。
2. 解释 prepare 与 launch 两个回调的分工：prepare 在计数区间开始前清空 L2 缓存（冷缓存测量），launch 发射被测 kernel 并可被安全地重复执行。
3. 读懂 `CUPTI_METRICS` 里 12 个指标名的含义（时长、DRAM/L2 流量、SM 吞吐占比、tensor 管线占比等）。
4. 说清团队约定：**运行器只落盘原始指标名和数值，一切解释（带宽、利用率、MFU）放到离线分析层**。

## 2. 前置知识

前两讲（u1-l1、u1-l2）已经建立了这些认知，这里直接承接、不再展开：

- `pegainfer-cupti` 是 Rust 通向 NVIDIA CUPTI Range Profiler 的桥，唯一公开函数是 `profile_range_with_prepare`；指标较多时 CUPTI 会**多遍回放**（replay）被测区间，所以 launch 闭包必须可以安全重复执行。
- 这个 crate 只有四个文件，CUPTI 通过 `kernel-report` feature + optional 依赖 + `required-features` 三层门控挡在默认构建图之外。

本讲新增的几个术语：

| 术语 | 通俗解释 |
|------|----------|
| decode attention（解码注意力） | 推理时逐 token 生成阶段执行的注意力 kernel，每次只算一个新 token 对全部历史 KV 的注意力 |
| 冷缓存（cold L2） | 测量前把 GPU 的 L2 缓存清空，让 kernel 面对的是"第一次读到数据"的状态。Qwen3 的 decode attention 主要在读 KV cache，生产环境中这些读取接近流式，冷缓存才是有代表性的测量条件 |
| 硬件计数器指标名 | CUPTI 的指标名形如 `域__计数器.聚合方式.限定词`，例如 `dram__bytes.sum` = DRAM 域的 bytes 计数器取求和；`sm__throughput.avg.pct_of_peak_sustained_elapsed` = SM 吞吐取平均、以峰值百分比表达 |
| 区间（range） | Range Profiler 的测量单位：一段被命名的 GPU 工作集合，计数器只统计落在这个区间里的 kernel |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-qwen3/src/bin/qwen3_kernel_report.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs) | 消费方：manifest 驱动的 per-op kernel 报告运行器，本讲的主角。含 `CUPTI_METRICS`、四个 `measure_cupti_*` 函数和 `cupti_metric_map` |
| [pegainfer-qwen3/src/bin/report_support/common.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs) | 报告工具共享原语：设备常量、`L2CacheClear`（L2 清扫）、带宽查询 |
| [pegainfer-qwen3/src/bin/report_support/decode_attention.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/decode_attention.rs) | decode attention 测量 case 定义；`DecodePath::name` 决定 range 名里的变体段 |
| [pegainfer-cupti/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs) | 被消费方：把 12 个 C ABI 裸参数收敛成一个带闭包的 unsafe 函数 |
| [docs/subsystems/kernels/kernel-op-reports.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md) | 这套工具的设计与演化记录：「只存原始指标」约定的出处 |
| [pegainfer-qwen3/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/Cargo.toml) | 三层门控的落点：optional 依赖、`kernel-report` feature、bin 的 `required-features` |

## 4. 核心概念与源码讲解

### 4.1 调用模板：`measure_cupti_decode` 的五个要素

#### 4.1.1 概念说明

`profile_range_with_prepare` 是个 `unsafe` 函数，签名在 [pegainfer-cupti/src/lib.rs:79-92](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/src/lib.rs#L79-L92)：文档注释写明安全契约——`context` 必须是对应 `device_index` 上活着的 CUDA `CUcontext`，且回调只能在该 context 上发射工作。函数本体我们已经知道：闭包经蹦床回传、输出缓冲变 `Vec<f64>` 返回值、错误合并成 `CuptiProfilerError`。

站在**消费者**的角度，每次调用只需要凑齐五个要素：

1. **context**：裸的 `CUcontext` 指针（`case.cu_context_ptr()`）；
2. **device_index**：设备序号（`case.ctx.device_ordinal`）；
3. **range 名**：一个人类可读字符串，标识这段被测区间；
4. **指标列表**：`CUPTI_METRICS`，本讲 4.3 展开；
5. **prepare / launch 两个闭包**：都返回 `Result<(), String>`，错误用字符串回传。

#### 4.1.2 核心流程

以 `measure_cupti_decode` 为例，从函数入口到拿到 `BTreeMap<String, f64>` 共八步：

```text
1. AttentionDecodeCase::for_spec(spec)     分配 case（KV cache、Q/K/V 缓冲等合成数据）
2. case.launch_once(path) + sync           预热一次：让 CUDA/FlashInfer 的惰性初始化
                                           发生在计数区间之外
3. L2CacheClear::new(&case.ctx)            准备 L2 清扫器（4.2 展开）
4. 取出三个"逃生"变量：
     context       = case.cu_context_ptr()          （裸指针，第 1 要素）
     device_ordinal = case.ctx.device_ordinal       （第 2 要素）
     clear_ctx     = case.ctx.clone()               （给 prepare 闭包用）
5. range_name = "qk/{path名}/b{batch}/k{kv_len}"    （第 3 要素）
6. prepare 闭包 = 清 L2；launch 闭包 = 再发射一次被测 kernel（第 5 要素）
7. unsafe { profile_range_with_prepare(context, device_ordinal, &range_name,
                                       CUPTI_METRICS, Some(&mut prepare), &mut launch) }
8. Ok(cupti_metric_map(&values))           Vec<f64> → BTreeMap<指标名, 值>（4.4 展开）
```

注意第 6 步的 launch 闭包与第 2 步的预热是**同一个动作**（`launch_once`）——预热在区间外执行一次，回放循环里每次 pass 再执行一次。这正是 u1-l1 说的"launch 闭包必须可重复安全执行"在消费端的实现方式：`launch_once` 不改变输入缓冲，重复发射结果一致。

#### 4.1.3 源码精读

入口与预热——先构造 case、发射一次并同步，把惰性初始化挡在测量之外：

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:1372-1374](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1372-L1374) 这三行构造 decode case、预发射一次 kernel 并同步，确保后续 CUPTI 计数不被 CUDA/FlashInfer 的惰性初始化污染。

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:1376-1385](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1376-L1385) 这段准备 L2 清扫器，取出 context、device_ordinal、克隆一份 clear_ctx，并把 range 名拼成 `qk/{变体名}/b{batch}/k{kv_len}` 的格式。

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:1387-1392](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1387-L1392) 这两个闭包就是模板的第 5 要素：`prepare` 只做一件事——清 L2；`launch` 调 `case.launch_once(path)` 发射被测 kernel。两者都把 `anyhow::Error` 链压平成 `String`（`format!("{err:#}")`），因为跨 FFI 只能传字符串。

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:1393-1402](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1393-L1402) 这是 `unsafe` 调用点本体：五个要素按顺序传入，返回的 `Vec<f64>` 与 `CUPTI_METRICS` 等长（pegainfer-cupti 侧预分配了同样长度的输出缓冲）。

range 名里的变体段来自 [pegainfer-qwen3/src/bin/report_support/decode_attention.rs:76-80](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/decode_attention.rs#L76-L80) ——这段代码把 `DecodePath` 映射成 `non_partition` 或 split-KV 配置的 label，所以本讲实践命令里的 `--variants non_partition` 对应的 range 名就是 `qk/non_partition/b1/k1024`。

同样的模板在文件里还有三份拷贝，只是 case 类型和 range 前缀不同：

| 函数 | 位置 | range 名格式 | case 持有方式 |
|------|------|--------------|----------------|
| `measure_cupti_dense` | [qwen3_kernel_report.rs:1079-1114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1079-L1114) | `qd/{op名}-{kernel类型}/r{行数}` | `RefCell` |
| `measure_cupti_decode` | [qwen3_kernel_report.rs:1368-1405](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1368-L1405) | `qk/{路径名}/b{batch}/k{kv长度}` | 直接 move 进 launch 闭包 |
| `measure_cupti_prefill` | [qwen3_kernel_report.rs:1407-1451](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1407-L1451) | `qpf/{阶段}-{变体}/b{batch}/s{序列长}` | `RefCell`（prepare 和 launch 都要用 case） |
| `measure_cupti_single_prefill` | [qwen3_kernel_report.rs:1453-1489](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1453-L1489) | `qpf/single/b{batch}/s{序列长}` | `RefCell` |

prefill 的 range 名里 `{阶段}` 取 `full`/`qk`/`kv`/`attn`、`{变体}` 取 `auto`/`q{tile}`，映射见 [report_support/prefill_attention.rs:70-75](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/prefill_attention.rs#L70-L75) 与 [report_support/prefill_attention.rs:103-109](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/prefill_attention.rs#L103-L109) ——这两段分别把 FA2 tile 变体和 prefill 阶段翻译成 range 名里的短标签。prefill 的 prepare 闭包比 decode 多一步（先 `prepare_stage` 重建阶段前置输入再清 L2），见 [qwen3_kernel_report.rs:1426-1433](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1426-L1433) ——这段代码在清 L2 之前先为被测阶段准备合法输入，让每个阶段报告只绑定一个目标 kernel。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认四个 `measure_cupti_*` 函数共享同一模板，并理解 case 持有方式的差异。
2. **操作步骤**：打开 `pegainfer-qwen3/src/bin/qwen3_kernel_report.rs`，依次跳到 L1079、L1368、L1407、L1453，给每个函数的三处加行内注释：`(a) 预热发射`、`(b) range 名`、`(c) profile 调用`。
3. **需要观察的现象**：decode 版本把 `case` 直接 move 进 launch 闭包（L1392），而另外三个版本用 `RefCell` + `borrow_mut()`。
4. **预期结果**：decode 的 prepare 闭包完全不碰 `case`（只碰 `cache_clear`），所以 case 可以整个交给 launch；prefill 的 prepare 要调 `prepare_stage`、launch 要调 `launch_stage`，两者都要可变借用，只能用 `RefCell` 在两个闭包间共享。
5. 本实践为纯阅读，无运行依赖。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `measure_cupti_decode` 在构造闭包之前要先 `case.launch_once(path)?; case.ctx.sync()?;`（L1373-1374）？

**参考答案**：这是预热发射。CUDA 驱动和 FlashInfer kernel 都有惰性初始化（模块加载、句柄创建等），这些开销一旦落进测量区间就会污染计数器。先在区间外发射一次并同步，回放循环里每次 pass 计到的就只有 kernel 本体。这与报告 schema 里记录的 `pre_measure_launches: 1`（[qwen3_kernel_report.rs:939](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L939)）对应。

**练习 2**：launch 闭包捕获了 `case`，而 CUPTI 可能多 pass 重放导致它被调用多次。为什么这里是安全的？

**参考答案**：`launch_once` 只是重复发射同一个 kernel，输入缓冲（Q/K/V、KV cache 页表）在闭包外构造一次后不再变化，重复发射幂等。多 pass 重放正是靠这个性质工作的：每个 pass 发射的工作必须相同，硬件计数器才能按指标分组分别累加。

**练习 3**：两个闭包的返回类型为什么是 `Result<(), String>` 而不是 `anyhow::Result<()>`？

**参考答案**：闭包要跨 FFI 边界被 C++ 侧回调（经 u2-l5/u3-l1 讲的蹦床）。蹦床把 `Ok(())` 转成 `0`、把 `Err` 转成 `1` 并把错误字符串暂存到 `CallbackState.error`，任何 Rust 错误类型都无法直接穿过 C ABI，所以消费端先用 `format!("{err:#}")` 把 anyhow 错误链压平成字符串。

### 4.2 prepare 闭包：`L2CacheClear` 与冷缓存测量

#### 4.2.1 概念说明

prepare 回调回答的问题是："**在开始计数之前，把测量环境整理成什么样？**"。对这个工具来说答案是：清空 L2 缓存。原因有二：

1. decode attention 的主要工作是读 KV cache。若上一轮测量的数据还留在 L2 里，kernel 会"免费"命中缓存，测出的带宽和时长都偏乐观，不代表生产环境（KV 读取接近流式，缓存里不会有现成数据）。
2. CUDA 官方的 `cudaCtxResetPersistingL2Cache` 系列接口只是把"持久化 L2 行"重置回普通状态，**不能驱逐普通缓存行**，所以只能用一个大数据量的流式 kernel 把 L2 里的旧数据"冲"出去。

#### 4.2.2 核心流程

```text
L2CacheClear::new(ctx):
  1. 查询设备 L2 大小（CU_DEVICE_ATTRIBUTE_L2_CACHE_SIZE）
  2. clear_bytes = max(2 × L2字节数, 128 MiB)     ← 两个常量：倍数 2、下限 128MiB
  3. len = clear_bytes / 2                          （bf16 每元素 2 字节）
  4. 分配三个长度为 len 的 bf16 缓冲：a、b、out

L2CacheClear::clear(ctx):                          （每次测量前调用）
  在设备流上发射 add_cuda(a, b, out, len)          ← 一次全量大数组加法，
                                                    读 a+b、写 out，各 clear_bytes 字节，
                                                    足以把 L2 旧内容全部替换掉
```

清多少字节才够？L2 容量为 \( C \)，清扫 kernel 的显存流量至少要 \( \geq C \) 才能把旧内容挤出去，代码取了 \( \max(2C,\ 128\,\text{MiB}) \) 留出裕量。

#### 4.2.3 源码精读

[pegainfer-qwen3/src/bin/report_support/common.rs:62-84](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs#L62-L84) 这段定义 `L2CacheClear` 结构并实现 `new`：查询 L2 大小，按 `cache_clear_bytes` 计算元素数，分配三个 bf16 缓冲（两个输入 + 一个输出）。

[pegainfer-qwen3/src/bin/report_support/common.rs:86-103](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs#L86-L103) 这段是 `clear` 本体：注释解释了为什么用流式 kernel 而不是 CUDA 的持久化 L2 重置 API；随后取三个缓冲的设备指针，调用 FFI 的 `add_cuda` 做一次全量向量加法，把 L2 旧内容冲掉。

[pegainfer-qwen3/src/bin/report_support/common.rs:25-26](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs#L25-L26) 这两行是清扫规模的常量：L2 大小的 2 倍、且不低于 128 MiB。

[pegainfer-qwen3/src/bin/report_support/common.rs:106-108](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs#L106-L108) 这个小函数把上面的规则封装成 `cache_clear_bytes(l2_bytes)`，报告 schema 里的 `cache_state: "l2_cleared_by_sweep"`（[qwen3_kernel_report.rs:938](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L938)）描述的就是这套机制。

回看消费端：prepare 闭包只调 `cache_clear.clear(&clear_ctx)`（[qwen3_kernel_report.rs:1387-1391](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1387-L1391)）。CUPTI 侧会在**每个回放 pass、区间开始之前**执行它（时序细节在 u2-l3 精读），所以每一遍计数面对的都是冷缓存。另外注意 `clear_ctx` 是 `case.ctx` 的克隆——闭包需要的句柄要么 move 进去、要么克隆一份，不能借用局部变量。

#### 4.2.4 代码实践（计算 + 阅读）

1. **实践目标**：验证自己能算出清扫缓冲规模，并读懂 `clear` 里的设计取舍注释。
2. **操作步骤**：
   - 假设设备 A 的 L2 为 32 MiB、设备 B 的 L2 为 80 MiB，分别算 `cache_clear_bytes` 和 `len`（bf16 元素数）。
   - 阅读 [common.rs:86-103](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/report_support/common.rs#L86-L103) 的注释，用一句话复述"为什么不用 `cuCtxResetPersistingL2Cache`"。
3. **需要观察的现象**：设备 A 触发 128 MiB 下限，设备 B 触发 2 倍规则。
4. **预期结果**：A：\( \max(64\,\text{MiB}, 128\,\text{MiB}) = 128\,\text{MiB} \)，\( len = 128\,\text{MiB} / 2 = 67{,}108{,}864 \) 个 bf16；B：\( \max(160\,\text{MiB}, 128\,\text{MiB}) = 160\,\text{MiB} \)，\( len = 83{,}886{,}080 \)。注释复述要点：该 API 只重置"持久化"行，不驱逐普通缓存行，因此必须用清扫 kernel。
5. 本实践为手算 + 纯阅读，无运行依赖。

#### 4.2.5 小练习与答案

**练习 1**：如果去掉 `CACHE_CLEAR_MIN_BYTES` 下限，在一块 L2 只有 1 MiB 的小卡上会发生什么？

**参考答案**：清扫流量变成 2 MiB，虽然理论上是 L2 的两倍，但过小的单次访问可能不足以把 L2 中所有旧数据行都替换掉（缓存替换策略未必是严格 LRU 全覆盖），且清扫 kernel 本身的启动开销占比变大。128 MiB 下限保证了"冲刷"的确定性。

**练习 2**：prepare 回调在 `profile_range_with_prepare` 内部被执行的时机是在计数区间的内部还是外部？为什么这个位置很重要？

**参考答案**：外部——每个 pass 在 `cuptiRangeProfilerStart`、`PushRange` 之前执行 prepare（u2-l3 会精读这条时序）。如果清 L2 的清扫 kernel 落在区间内部，它自己的 DRAM/L2 流量和执行时间都会被计入计数器，测量结果就不再只属于被测 kernel。

### 4.3 `CUPTI_METRICS` 指标清单

#### 4.3.1 概念说明

`CUPTI_METRICS` 是一个 12 项的 `&[&str]` 常量，定义了每次采样要哪些硬件计数器换算出的指标。指标名遵循 `域__计数器.聚合.限定` 的命名法。这 12 个可以分成五组，覆盖"这个 kernel 慢在哪"的完整画像：

| 组 | 指标名 | 含义 |
|----|--------|------|
| 时间 | `gpu__time_duration.sum` | 区间内 GPU 工作总时长（文档示例按微秒记录，如 `4043.648us`） |
| 时间 | `sm__cycles_elapsed.avg.per_second` | SM 平均时钟频率（GHz），把周期数换算成时间的桥梁 |
| 显存流量 | `dram__bytes.sum` / `dram__bytes_op_read.sum` / `dram__bytes_op_write.sum` | DRAM 总流量 / 读流量 / 写流量（字节） |
| 缓存流量 | `lts__t_bytes.sum` | L2 cache（LTS 段）的总流量（字节） |
| 利用率 | `sm__throughput.avg.pct_of_peak_sustained_elapsed` | SM 吞吐占峰值百分比（SOL 类指标） |
| 利用率 | `smsp__warps_active.avg.pct_of_peak_sustained_active` | 活跃 warp 占比（occupancy 的直接读数） |
| tensor 管线 | `sm__pipe_tensor_cycles_active.avg.pct_of_peak_sustained_elapsed` | tensor 管线活跃周期占比 |
| tensor 管线 | `sm__pipe_tensor_subpipe_hmma_cycles_active.avg.pct_of_peak_sustained_elapsed` | 其中 HMMA 子管线的占比 |
| tensor 管线 | `sm__ops_path_tensor_op_hmma_src_bf16_dst_fp32_sparsity_off.sum.pct_of_peak_sustained_elapsed` | BF16→FP32、无稀疏 HMMA 操作数占峰值百分比——**可直接当 MFU 读** |
| tensor 管线 | 同上 `.sum.per_second` | 每秒完成的该类 HMMA 操作数（如 `211.727e12`） |

值得注意：时间/字节是**绝对量**，其余是**百分比**，可以互相印证——例如 DRAM 带宽 \( = \texttt{dram\_\_bytes.sum} / \texttt{gpu\_\_time\_duration.sum} \)，再对照峰值带宽就能手算"带宽利用率"，这正是 4.4 说的离线分析层要做的事。

#### 4.3.2 核心流程

指标列表的生命周期：

```text
常量定义 CUPTI_METRICS (12 项)
  ├─ 传入 profile_range_with_prepare → C++ 侧编译成 config image → 分组安排到 N 个 pass
  └─ 返回的 Vec<f64> 与列表按位置一一对应
       └─ cupti_metric_map zip 成 (指标名, 值) → 落盘 JSON
```

指标越多，pass 越多（硬件同时可用的计数器组有限）——这是 12 项指标换来更多回放次数的取舍。

#### 4.3.3 源码精读

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:149-162](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L149-L162) 这 14 行就是完整的指标清单，顺序即返回 `Vec<f64>` 的下标顺序。前 7 项是基础画像（时长、频率、DRAM/L2、SM/warp），后 5 项是 tensor 管线指标。

后 5 项是后来补的：[docs/subsystems/kernels/kernel-op-reports.md:207-214](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L207-L214) 记录了这次变更——作者先用 `ncu --query-metrics` 探查本机（RTX 5090）的指标目录，然后**把直接的 tensor 峰值百分比指标加进默认列表，而不是在运行器里算 MFU**。[kernel-op-reports.md:224](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L224) 给出结论：BF16-HMMA 峰值百分比指标本身就是答案，外部规格表推算的 MFU 只做 sanity check。这再次体现了"让硬件说硬件的话，解释留给离线层"的原则。

#### 4.3.4 代码实践（解读型）

1. **实践目标**：用文档里已存档的真实测量值做一次"读数训练"，学会从指标组合判断瓶颈。
2. **操作步骤**：阅读 [kernel-op-reports.md:180-184](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L180-L184) 中 `seq_len=10000` 时四个 prefill 阶段的 CUPTI 摘要，重点对比 `attn`（attention core）这一行。
3. **需要观察的现象**：attention core 行里 `L2 6650MB` 远大于 `DRAM 205MB`；`active warps 16.020%` 远低于 `SM throughput 41.410%`。
4. **预期结果**：得出两个方向性结论—— 显存带宽不是瓶颈（L2 命中率极高，DRAM 流量很小）； 瓶颈是占用率太低（活跃 warp 只有 16%，tensor 管线喂不满）。这与 [kernel-op-reports.md:185](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L185) 的记录一致，后续 NCU 分析（[kernel-op-reports.md:233-241](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L233-L241)）把低占用的原因定位到寄存器和共享内存占用过高。
5. 本实践为纯阅读文档，无运行依赖。

#### 4.3.5 小练习与答案

**练习 1**：`dram__bytes.sum` 与 `lts__t_bytes.sum` 分别在哪一级？为什么 attention core 的 L2 流量是 DRAM 的三十多倍反而是好事？

**参考答案**：`dram__*` 在显存（DRAM）级，`lts__*` 在 L2 cache 级。KV cache 在多轮迭代间被反复读，第二遍读取由 L2 命中，不必再下探 DRAM——L2 流量大说明数据复用发生在片上，显存带宽没有被浪费。真正要警惕的是反过来的情形：DRAM 流量逼近带宽上限。

**练习 2**：`smsp__warps_active...pct_of_peak_sustained_active` 测的是占用率，`sm__throughput...pct_of_peak_sustained_elapsed` 测的是吞吐。两者的差值（吞吐高、占用低）说明了什么？

**参考答案**：说明每个驻留 warp 都很"忙"（在持续发射指令），但驻留的 warp 数太少，硬件的多线程能力没用满。这正是 attention core 的处境：寄存器/共享内存限制让每块只能驻留很少 warp，tensor 管线只能靠这少数 warp 喂——优化方向是降低资源占用提升驻留数，而不是改访存。

**练习 3**：如果要给自己的 kernel 只选 3 个指标做快速体检，选哪三个？为什么？

**参考答案**：示例选法（合理即可）：`gpu__time_duration.sum`（时长基准）、`dram__bytes.sum`（算带宽是否到顶）、`smsp__warps_active.avg.pct_of_peak_sustained_active`（是否被占用率卡住）。三者分别覆盖时间、显存、执行资源三个维度，足够判断"慢在显存还是慢在执行"。

### 4.4 `cupti_metric_map` 与原始指标落盘约定

#### 4.4.1 概念说明

`profile_range_with_prepare` 返回的 `Vec<f64>` **不带名字**——第 i 个值对应 `CUPTI_METRICS[i]`，仅此而已。`cupti_metric_map` 负责把这层位置对应变成 `BTreeMap<String, f64>`，随后整个 map 原样写进报告 JSON 的 `case.cupti` 字段。

团队的硬约定是：**运行器不做任何解释**。早期版本曾有 `diagnosis` 字段、算好的 `dram_gb_s`/`dram_peak_pct` 等派生值，后来全部删掉（schema 升到 4）。理由：解释（带宽百分比、利用率标签、放大率门槛）依赖硬件与问题上下文，放进运行器会让 schema 反复变动、compare 逻辑被解释规则绑架；存原始值 + 离线解释则让同一份 JSON 可以被反复重新解读。

#### 4.4.2 核心流程

从 `Vec<f64>` 到 JSON 的完整数据流：

```text
profile_range_with_prepare → Vec<f64>（长度 = 12）
  ↓ cupti_metric_map：CUPTI_METRICS.iter().zip(values.iter()) → BTreeMap
CaseResult.cupti: Option<BTreeMap<String, f64>>
  ↓ cases 聚合进 KernelSnapshot
JSON: cases[].cupti = { "gpu__time_duration.sum": 4043.648, ... }
  ↓ 离线层（rank 子命令 / 人工 / 笔记本）按需推导带宽、SoL%、MFU
```

同时快照的 `measurement` 段记录测量条件（[qwen3_kernel_report.rs:936-950](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L936-L950)）：`cupti_enabled`、开启时的 `cupti_metrics` 名单、`cache_state`、`iters` 等——让离线层知道"这份数据是在什么条件下采的"。

#### 4.4.3 源码精读

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:1491-1498](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L1491-L1498) 这六行就是 `cupti_metric_map` 的全部：按位置 zip 指标名和数值，收集成 `BTreeMap`（键有序，落盘 JSON 里指标按字典序排列，diff 友好）。四个 `measure_cupti_*` 的最后一行都是它。

[pegainfer-qwen3/src/bin/qwen3_kernel_report.rs:427-432](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L427-L432) 这段是 `CaseResult` 的字段定义，`cupti: Option<BTreeMap<String, f64>>` 就是 JSON 里 `case.cupti` 的来源；`Option` 对应"本 case 没开 CUPTI 或采样失败"两种情况。

采样开关的解析在 [qwen3_kernel_report.rs:596](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L596) 与 [qwen3_kernel_report.rs:637](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L637) ——两处都是 `cupti = cli.cupti || !cli.no_cupti`：**CUPTI 默认开启**，只有显式传 `--no-cupti` 才关闭（两个 flag 的定义在 [qwen3_kernel_report.rs:198-201](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/bin/qwen3_kernel_report.rs#L198-L201)）。

「只存原始指标」约定的出处：

- [kernel-op-reports.md:114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L114) ——写明"报告只存原始 CUPTI 指标名和数值，诊断标签、派生带宽百分比、读放大门槛都属于离线分析层"。
- [kernel-op-reports.md:130-134](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L130-L134) ——记录了删除 `diagnosis` 与派生字段、`compare` 只对 `latency_us` 设门槛的决策，schema 因此升到 4。
- [kernel-op-reports.md:469](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L469) ——教训总结："指标解释不进运行器；JSON 存原始 CUPTI 值，带宽/利用率/IO 模型检查放在独立的报告或笔记本层"。

#### 4.4.4 代码实践（离线分析演练）

1. **实践目标**：亲手扮演一次"离线解释层"，体会运行器/解释层分层的价值。
2. **操作步骤**：取 [kernel-op-reports.md:181](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L181) 中 QK+RoPE 阶段的数据（`gpu__time_duration.sum=285.664us`、`DRAM 203.183MB`），计算带宽（示例代码）：

   ```python
   # 示例代码：离线推导，不属于项目源码
   dram_bytes = 203.183e6
   duration_s = 285.664e-6
   gb_per_s = dram_bytes / duration_s / 1e9
   print(f"{gb_per_s:.1f} GB/s")   # 再除以设备峰值带宽即得占用百分比
   ```

3. **需要观察的现象**：算出的带宽值与文档记录的 `SM throughput 51.880%` 是两个独立维度——前者看显存，后者看执行单元。
4. **预期结果**：约 711 GB/s。注意这个数字若接近设备峰值，说明 QK+RoPE 受显存限制；若远低于峰值而 SM 吞吐也不高，则两头都没吃满，需要再看占用率指标。
5. 文档数值为已存档的 GPU 测量结果，本机复算只需手算/脚本，无需 GPU；**若要重新采样得到本机数值，待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cupti_metric_map` 用 `BTreeMap` 而不是 `HashMap`？

**参考答案**：`BTreeMap` 按键有序，序列化成 JSON 后指标顺序稳定，两次运行的结果可以直接做文本 diff 或机器对比；`HashMap` 顺序不定，会让报告噪音变大。

**练习 2**：`compare` 子命令为什么只对 `latency_us` 设回归门槛，而不对 CUPTI 指标设门槛？

**参考答案**：见 [kernel-op-reports.md:133](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md#L133)。计数器指标受时钟、驱动、温度等影响波动较大，且不同 GPU 的"正常值"完全不同；把解释性门槛放进 compare 会让回归门槛随硬件漂移。延迟是最终关心的量，门槛只看它；计数器留给离线诊断。

**练习 3**：`case.cupti` 是 `Option`。哪两种情况下它会是 `None`/缺失？

**参考答案**： 运行时传了 `--no-cupti`（`args.cupti == false`，四个 `measure_*_case` 里的 `if args.cupti` 分支不执行）； 采样过程中出错——错误会被写进 `case.error`，`cupti` 保持初始的 `None`。

## 5. 综合实践

**任务**：跑两次最小报告（开/关 CUPTI），对比 JSON，亲眼看到 `case.cupti` 的出现与消失——这是把本讲四个模块串起来的闭环。

### 有 GPU 与 CUDA 工具链的环境

1. **实践目标**：确认 `--no-cupti` 只影响 `cupti` 相关字段，其他测量（latency、measurement 元数据）照常工作。

2. **操作步骤**：

   ```bash
   # 第一次：显式关闭 CUPTI（本命令与 docs/subsystems/kernels/kernel-op-reports.md
   # 的 GPU 验证记录一致）
   cargo run --release -p pegainfer-qwen3 --features kernel-report \
     --bin qwen3_kernel_report -- run --no-cupti --iters 1 \
     --contexts 1024 --batch-sizes 1 --variants non_partition \
     --out /tmp/report_nocupti.json

   # 第二次：去掉 --no-cupti（CUPTI 默认开启）
   cargo run --release -p pegainfer-qwen3 --features kernel-report \
     --bin qwen3_kernel_report -- run --iters 1 \
     --contexts 1024 --batch-sizes 1 --variants non_partition \
     --out /tmp/report_cupti.json
   ```

   提示：若 `nvidia-smi` 不可用，按项目惯例加 `PEGAINFER_CUDA_SM=<架构号>`（如 `120`）环境变量。构建需要编译 CUDA 源，首次耗时较长，务必用 `--release`。

3. **需要观察的现象**（用编辑器或 `jq` 打开两份 JSON 对比）：
   - `measurement.cupti_enabled`：`false` vs `true`；
   - `measurement.cupti_metrics`：空数组 vs 12 个指标名的列表；
   - `cases[0].cupti`：缺失（`null`）vs 一个含 12 个键的 map，键名与 `CUPTI_METRICS` 完全一致、按键有序；
   - `cases[0].latency_us` 两份都有值（关闭 CUPTI 不影响延迟测量）；
   - `schema` 字段两份都是 `4`。

4. **预期结果**：两份 JSON 除上述 cupti 相关差异外结构一致；开启 CUPTI 的那次运行时间明显更长（每个 case 要建 CUPTI 会话并多 pass 回放）。上述现象的具体数值**待本地验证**。

### 没有 GPU 的环境（替代实践）

1. **实践目标**：不运行也能掌握调用模板与 range 命名规则。
2. **操作步骤**：在 `pegainfer-qwen3/src/bin/qwen3_kernel_report.rs` 中定位 L1079（`measure_cupti_dense`）、L1368（`measure_cupti_decode`）、L1407（`measure_cupti_prefill`）三处 `profile_range_with_prepare` 调用，为每处的 `range_name` 写一行注释，说明前缀（`qd/`、`qk/`、`qpf/`）和各段的来源（op 名/路径名/阶段-变体/形状参数）。
3. **需要观察的现象**：三处调用的参数结构完全相同，只有 case 类型、range 名和闭包内容不同。
4. **预期结果**：例如 `--variants non_partition --contexts 1024 --batch-sizes 1` 对应 range 名 `qk/non_partition/b1/k1024`；prefill attention core 的 `cta_q64` 变体在 `s=10000` 时对应 `qpf/attn-q64/b1/s10000`。

## 6. 本讲小结

- 消费端调用模板五要素：**context、device_index、range 名、指标列表、prepare/launch 闭包**；四个 `measure_cupti_*` 函数共享这一模板，差异只在 case 类型、range 前缀和闭包内容。
- **prepare 负责整理环境，launch 负责发射被测 kernel**：prepare 用 `L2CacheClear`（`max(2×L2, 128MiB)` 字节的流式加法）在每个 pass 的计数区间外清空 L2，保证冷缓存测量；launch 的 kernel 必须可重复发射，因为多 pass 回放会多次调用它。
- `CUPTI_METRICS` 的 12 个指标分五组：时长/频率、DRAM 流量、L2 流量、SM/warp 利用率、tensor 管线（含可直接当 MFU 读的 BF16-HMMA 峰值百分比）。
- **运行器只存原始指标**：`cupti_metric_map` 把按位置对应的 `Vec<f64>` 变成 `BTreeMap` 落盘到 `case.cupti`；带宽、利用率、诊断标签一律留在离线层，`compare` 只对延迟设门槛（schema 4）。
- 采样入口之外的两条工程护栏同样关键：预热发射挡住惰性初始化污染；`--no-cupti` 让没有 CUPTI 工具链的环境仍能跑延迟测量（CUPTI 默认开启）。

## 7. 下一步学习建议

本讲看的是"怎么用"，下一单元（u2）开始拆"怎么实现"。建议顺序：

1. **u2-l1（C ABI 契约）**：跳到 [pegainfer-cupti/csrc/range_profiler.cpp](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-cupti/csrc/range_profiler.cpp)，对照本讲的调用模板，看这五个要素在 C++ 侧如何变成 12 个裸参数，以及入口处的防御性校验。
2. **u2-l3（回放循环）**：验证本讲反复引用的事实——prepare 确实在 `Start`/`PushRange` 之前、每个 pass 执行一次；理解 `launch_once` 为何会被调用多次。
3. 顺带重读本讲的两个文档锚点：[kernel-op-reports.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/subsystems/kernels/kernel-op-reports.md) 的 Step 10（为什么删派生字段）与 Step 14（为什么加 tensor 指标）——它们是这个工具最重要的两条设计决策记录。
