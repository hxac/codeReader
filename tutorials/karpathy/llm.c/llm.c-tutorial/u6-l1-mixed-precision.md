# 混合精度、master weights 与 TF32

## 1. 本讲目标

本讲是「训练工程」单元的第一篇，聚焦 llm.c CUDA 主线 `train_gpt2.cu` 中**与数值精度有关的三个工程开关**。学完后你应当能够：

1. 说清 **FP32 / FP16 / BF16** 三种浮点格式的位宽、动态范围与精度的取舍，并能在 `Makefile` 里用 `PRECISION` 变量切换它们。
2. 解释 BF16 训练时为何还要保留一份 **FP32 master weights**，以及 `adamw` kernel 如何用 **随机舍入（stochastic rounding）** 把 fp32 更新结果无偏地写回 bf16。
3. 区分**数据精度**与**计算精度**两个概念，说清 **TF32** 只是 FP32 数据在 tensor core 上的一种「更快但尾数更短」的**计算模式**（而非一种存储格式），并能用 `-f` 开关控制它。

本讲只讨论「精度怎么选、怎么实现」，不展开任何 kernel 内部的并行细节（那属于 u5-l4），也不展开 ZeRO 多卡分片（属于 u6-l4）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **floatX 是编译期类型别名**：`train_gpt2.cu` 里所有权重/激活的类型都写成 `floatX`，由 `Makefile` 的 `PRECISION` 变量经 `-DENABLE_*` 宏在编译期定为 `float` / `half` / `__nv_bfloat16`（见 u5-l1）。
- **权重与优化器状态是分开的内存块**：`GPT2` 结构体里有 `params_memory`（模型权重）、`grads_memory`（梯度）、`m_memory`/`v_memory`（AdamW 的一阶/二阶动量），它们各自独立分配（见 u3-l2、u5-l1）。
- **AdamW 更新公式**：对每个参数 \(\theta\)，用梯度 \(g\)、动量 \(m,v\)、偏差修正后得到更新（见 u3-l2）

\[
\theta \leftarrow \theta - \eta\left(\frac{\hat m}{\sqrt{\hat v}+\varepsilon} + w_d\cdot \theta\right)
\]

- **cuBLASLt 是 matmul 的执行后端**：所有线性层最终都走 `matmul_cublaslt`（见 u5-l3），它有一个「计算类型（compute type）」参数决定累加用什么精度。

下面用一张表回顾三种浮点格式的关键差异，这是本讲全部分析的基础：

| 格式 | 符号 | 指数 | 尾数（含隐含 1） | 动态范围（量级） | 相对精度 | llm.c 别名 |
|------|------|------|------------------|------------------|----------|-----------|
| FP32 | 1 | 8 | 24 | \(\sim 10^{\pm38}\) | \(\sim 7\) 位十进制 | `float` |
| FP16 | 1 | 5 | 11 | \(\sim 10^{\pm4}\)（最大 65504） | \(\sim 3\) 位十进制 | `half` |
| BF16 | 1 | 8 | 8 | \(\sim 10^{\pm38}\)（与 FP32 同） | \(\sim 2\)–3 位十进制 | `__nv_bfloat16` |

> **关键直觉**：FP16 和 BF16 都是 16 位，但 FP16 把位宽花在「尾数（精度）」上，BF16 花在「指数（范围）」上。BF16 与 FP32 共享指数位宽，所以**动态范围相同**，梯度不会上溢/下溢，代价是尾数更短、精度更低。这就是 llm.c 默认选 BF16 而非 FP16 的根本原因。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 定义 `PRECISION` 变量，把它翻译成 `-DENABLE_*` 编译宏 `PFLAGS` |
| [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) | 用 `ENABLE_*` 宏在编译期把 `floatX` 定为三种类型之一，并定义 `PRECISION_MODE` 枚举 |
| [llmc/cublas_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h) | 定义 cuBLAS 的数据类型 `CUBLAS_LOWP` 与全局计算类型 `cublas_compute` |
| [llmc/adamw.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh) | AdamW kernel：读 master/bf16、fp32 更新、随机舍入写回 bf16、回写 fp32 master |
| [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) | `stochastic_rounding` 的三种重载（bf16/fp16/fp32） |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | `GPT2` 结构体里的 `master_weights` 字段、`gpt2_allocate_state` 分配、`common_start` 设置 TF32、`main` 的 `-f`/`-w` 开关 |

## 4. 核心概念与源码讲解

### 4.1 三种精度与编译选项：PRECISION → floatX

#### 4.1.1 概念说明

训练一个大模型时，「用什么精度存权重和激活」是一个核心工程决策。llm.c 把这个决策做成一个**编译期选项**，而不是运行时选项——因为切换精度意味着 `floatX` 这个类型别名整个换掉，源码里所有 `floatX*` 指针、所有 cuBLAS 的数据类型都要一起变，这只能在编译期完成。

llm.c 支持三种精度，对应三种编译宏：

- `ENABLE_FP32`：全 FP32 训练。最准、最慢、最吃显存。适合做正确性参照（`test_gpt2cu` 默认用 FP32 跑一遍）。
- `ENABLE_BF16`（**默认**）：混合精度主力。前向/反向用 BF16，优化器更新用 FP32（靠 master weights，见 4.2）。
- `ENABLE_FP16`：理论上支持的半精度，但 llm.c **没有实现梯度缩放（gradient scaler）**，而 FP16 窄动态范围极易让梯度上溢，所以实际几乎不用。

源码顶部一句注释把这个坑明确标了出来：

> `// use fp16 (note: this may require gradient scaler, currently not implemented!)`

这就是为什么默认是 BF16 而不是 FP16——BF16 与 FP32 动态范围相同，不需要 gradient scaler 也能稳定训练。

#### 4.1.2 核心流程

精度的传导链是**三级翻译**：

```text
Makefile:  PRECISION ?= BF16          （用户可改 make PRECISION=FP32）
            ↓ 翻译成宏
            PFLAGS = -DENABLE_BF16     （或 -DENABLE_FP32 / -DENABLE_FP16）
            ↓ 只加到主线目标 train_gpt2cu 的编译命令
cuda_common.h: #if defined(ENABLE_FP32) ... #elif defined(ENABLE_FP16) ... #else
            typedef __nv_bfloat16 floatX;   ← 整份源码的类型别名就定在这
```

注意一个重要边界：`PFLAGS` **只加到 `train_gpt2cu` / `test_gpt2cu` / `profile_gpt2cu` / `cudnn_att.o` 这些主线目标**，而**不加到 `train_gpt2fp32cu`**。后者是冻结的 fp32 legacy 学习版（见 u4-l3），它恒为 FP32，不接受 `PRECISION`。这是 Makefile 里一条容易看漏的规则。

#### 4.1.3 源码精读

**(1) Makefile：PRECISION → PFLAGS。** `Makefile:232-244` 把变量翻译成宏，并校验合法值（只允许 `FP32 FP16 BF16` 三者，写错直接 `$(error)` 报错）：

[Makefile:232-244](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L232-L244) —— 定义默认值 `PRECISION ?= BF16`、合法值白名单，以及把三个合法值分别映射到 `-DENABLE_FP32` / `-DENABLE_FP16` / `-DENABLE_BF16`（`PFLAGS`）。

然后只有主线 CUDA 目标用了 `$(PFLAGS)`：

[Makefile:273-274](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L273-L274) —— `train_gpt2cu` 的编译命令里带上了 `$(PFLAGS)`。

[Makefile:276-277](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L276-L277) —— 对比 `train_gpt2fp32cu` 的命令**没有** `$(PFLAGS)`，恒为 fp32 legacy。

**(2) cuda_common.h：宏 → floatX 类型别名。** 真正让 `floatX` 落地的是这段条件编译：

[cuda_common.h:75-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h#L75-L92) —— 定义 `PrecisionMode` 枚举，再用 `#if defined(ENABLE_FP32)/#elif defined(ENABLE_FP16)/#else` 三分支把 `floatX` 分别定为 `float` / `half` / `__nv_bfloat16`，并同步设置 `PRECISION_MODE` 常量（FP32/FP16/BF16）。`#else` 分支即默认的 BF16。

这一段是「精度无关编程」的根基：因为整份 `train_gpt2.cu` 和 `llmc/` 头文件都写 `floatX` 而不是具体类型，所以一次 `make PRECISION=...` 就能让全代码库换精度，而无需改任何业务逻辑。

**(3) main 里默认加载 bf16 权重。** 因为默认是 BF16，`main` 里默认的 checkpoint 文件名也是 bf16 版：

[train_gpt2.cu:1423](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1423) —— `const char* load_filename = "gpt2_124M_bf16.bin";`（如果你改 `PRECISION=FP32`，就得同时换成 `gpt2_124M.bin` 这种 fp32 权重，否则读出的位会被当成 bf16 误解释）。

**(4) 启动时打印当前精度。** `main` 把 `PRECISION_MODE` 翻译成人话打印出来：

[train_gpt2.cu:1551-1553](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1551-L1553) —— 注意 FP32 分支会进一步区分「开了 TF32」还是「纯 FP32」（`cublas_compute` 是否等于 `CUBLAS_COMPUTE_32F_FAST_TF32`），这正是 4.3 节要讲的 TF32 开关。

#### 4.1.4 代码实践

**实践目标**：亲手切换一次精度，观察编译期类型别名和运行时打印的变化。

**操作步骤**：

1. 默认编译（BF16）：`make train_gpt2cu`，运行 `./train_gpt2cu`，观察启动表格里 `precision` 一行打印 `BF16`。
2. 重新用 FP32 编译：`make clean && make train_gpt2cu PRECISION=FP32`。注意此时需要 fp32 权重文件；若手头只有 `gpt2_124M_bf16.bin`，可以改用 `-e gpt2_124M.bin` 指向一个 fp32 权重（参见 [train_gpt2.cu:1466](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1466) 的 `-e` 参数）。
3. 再次运行，观察 `precision` 行打印 `FP32` 或 `TF32`（取决于 GPU 架构与 `-f`，见 4.3）。
4. 试一个非法值：`make train_gpt2cu PRECISION=INT8`，应当看到 Makefile 报 `Invalid precision INT8` 并中止。

**需要观察的现象**：

- 切到 FP32 后，`gpt2_allocate_state` 打印的「parameter gradients」显存（见 [train_gpt2.cu:368](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L368)）会比 BF16 时**翻倍**（fp32 每元素 4 字节 vs bf16 的 2 字节）。这是混合精度省显存最直观的证据。

**预期结果**：BF16 训练时权重+梯度共占 `2 * num_params` 字节量级的 bf16，而 FP32 是 `2 * num_params * 4` 字节。对于 124M 模型，仅权重+梯度就相差数百 MB。

**待本地验证**：若你没有 GPU，无法运行 `train_gpt2cu`，可以退化为「源码阅读型实践」——在 `cuda_common.h` 与 `Makefile` 之间手工模拟一遍「`PRECISION=FP32` → `PFLAGS=-DENABLE_FP32` → `typedef float floatX`」这条链，确认每一步对应的行号。

#### 4.1.5 小练习与答案

**练习 1**：为什么 llm.c 默认用 BF16 而不是 FP16？

**参考答案**：FP16 的指数位只有 5 位，动态范围很窄（最大约 65504），反向传播算出的梯度极易上溢成 `inf`，需要配套的 **gradient scaler** 动态放大 loss、再缩小梯度来规避。llm.c 顶部注释明说 gradient scaler「currently not implemented」，所以 FP16 路径不安全。BF16 与 FP32 共享 8 位指数，动态范围相同，梯度不会上溢，因此无需 scaler 即可稳定训练，是更稳妥的默认选择。

**练习 2**：`make train_gpt2fp32cu PRECISION=BF16` 会让 legacy 版变成 bf16 吗？

**参考答案**：不会。`train_gpt2fp32cu` 的编译命令（`Makefile:276-277`）里没有 `$(PFLAGS)`，所以 `PRECISION` 变量对它完全无效，它永远是冻结的 fp32。`PRECISION` 只对主线 `train_gpt2cu` 生效。

---

### 4.2 master weights：FP32 备份与随机舍入

#### 4.2.1 概念说明

混合精度训练的核心难题是一个**「微小更新被吞掉」**的问题。考虑一次 AdamW 更新，学习率 \(\eta=10^{-4}\) 量级，归一化后的步长 \(m/\sqrt{v}\) 大约在 \(0.1\) 量级，那么一次参数更新量约为

\[
\Delta\theta \approx \eta \cdot \frac{\hat m}{\sqrt{\hat v}+\varepsilon} \approx 10^{-5}
\]

而一个权重 \(\theta\) 本身可能是 \(0.5\) 量级。BF16 的尾数只有 7 位有效位（\(2^{-7}\approx 0.0078\)），也就是说 \(\theta + \Delta\theta = 0.50001\) 在 BF16 里**根本无法与 \(0.5\) 区分**——这次更新会被直接舍掉，等于这一步白训了。

经典解法（来自 NVIDIA 的 *Mixed Precision Training*, 2018）是维护**三份权重**：

1. **FP32 master weights**：优化器真正持有、更新的「真值」，全精度。
2. **BF16 weights**（`params_memory`）：前向/反向实际用的「工作副本」，从 master 舍入而来。
3. 前向/反向在 BF16 上跑（快、省显存），优化器更新在 FP32 master 上跑（准），更新完再把 master 随机舍入回 BF16 给下一步前向用。

这样，哪怕单步 \(\Delta\theta\) 小到 BF16 装不下，它在 FP32 master 里仍被忠实累加；多步之后累积的更新一旦大到能被 BF16 表示，就会真正反映到工作副本上。这就是「BF16 训练时为何仍保留 fp32 master weights」的根本原因。

第二个关键技巧是**随机舍入（stochastic rounding）**。把 FP32 的 master 值舍入成 BF16 时，如果总是「四舍五入」（最近舍入），那么处于两个可表示值正中间的小更新可能系统性地被向同一边舍、产生偏差。随机舍入的做法是：以概率正比于「到上一个可表示值的距离」来决定向上还是向下取，使得

\[
\mathbb{E}[\mathrm{round}(x)] = x
\]

即**舍入是无偏的**。这样即便单次舍入有误差，期望上更新量被完整保留，长期累加不会偏。

#### 4.2.2 核心流程

一次「前向→反向→更新」里，master weights 的生命周期如下：

```text
前向 / 反向：   读 params_memory (bf16)  ← 工作副本
                                         
gpt2_update：
  1. old_param = master_weights[idx]            ← 读 FP32 真值（若 master 存在）
                  若 master 不存在则 (float)params_memory[idx]（直接读 bf16，有损）
  2. param = old_param - lr*(m_hat/(sqrt(v_hat)+eps) + wd*old_param)   ← FP32 运算
  3. stochastic_rounding(param, &params_memory[idx])   ← 无偏舍入写回 bf16 工作副本
  4. master_weights[idx] = param                ← 回写 FP32 master，供下次更新
```

可以看到，master weights 把「优化器算什么」和「网络前向用什么」**解耦**了：优化器永远在 FP32 上算，网络前向永远在 BF16 上跑，两者通过一次随机舍入同步。

#### 4.2.3 源码精读

**(1) GPT2 结构体里的两个字段。** master weights 的「开关」和「指针」都挂在 `GPT2` 上：

[train_gpt2.cu:300](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L300) —— `float* master_weights;`，注释明确「is NULL unless fp32 weights is enabled」（注意类型是 `float*`，恒为 FP32，与 `floatX` 无关）。

[train_gpt2.cu:315](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L315) —— `int use_master_weights;`，0/1 开关，决定是否维护这份 FP32 副本。

**(2) 安全默认值。** 初始化时默认开启 master weights：

[train_gpt2.cu:347](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L347) —— `model->use_master_weights = 1;`，注释「safe default: do keep master weights in fp32」。

**(3) 按 FP32 分配 master 缓冲。** master weights 的显存单独分配，且**单位是 `sizeof(float)`（4 字节）**，与当前 `floatX` 无关——这正是「master 恒为 FP32」的体现：

[train_gpt2.cu:405-409](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L405-L409) —— 当 `use_master_weights == 1` 时，分配 `shard_num_parameters * sizeof(float)` 字节。对比一下：紧邻它的 `m_memory`/`v_memory`（[train_gpt2.cu:402-403](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L402-L403)）也是 `sizeof(float)`，因为 AdamW 的动量状态同样必须用 FP32 才准。

**(4) AdamW kernel 的核心四步。** 这是 master weights 机制的心脏：

[adamw.cuh:37-46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L37-L46) —— 逐行对应 4.2.2 的流程：第 38 行读 `old_param`（有 master 用 master，否则退化为读 bf16）；第 40 行做 FP32 的 AdamW 更新；第 43 行 `stochastic_rounding` 把结果无偏舍入写回 `params_memory`（bf16 工作副本）；第 46 行把 FP32 的 `param` 回写 master。

注意第 38 行那个三元表达式：它让同一个 kernel **同时支持「有 master」和「无 master」两种模式**。关掉 master（`-w 0`）时，优化器直接在 bf16 上更新，省一份显存，但精度有损——这正是「省显存 vs 保精度」的取舍。

**(5) 三种 stochastic_rounding 重载。** 随机舍入按目标类型分了三个版本：

[cuda_utils.cuh:269-284](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L269-L284) ——
- BF16 版（269-278）：用 `Get2dNoiseUint` 给每个线程一个独立随机数，取出低 16 位作阈值，与 FP32 值的低 16 位比较来决定向上/向下取整——这是真正实现的无偏随机舍入；
- FP16 版（279-281）：注释写着 `// todo - implement this...`，目前只是普通赋值（即未真正实现随机舍入，这也是 FP16 路径不成熟的又一证据）；
- FP32 版（282-284）：恒等函数（FP32→FP32 无需舍入）。

**(6) 运行时开关 `-w`。** `main` 提供了命令行开关，默认开：

[train_gpt2.cu:1446-1447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1446-L1447) —— `override_enable_tf32` 与 `use_master_weights` 都默认为 1。

[train_gpt2.cu:1485-1486](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1485-L1486) —— `-f` 控制 TF32、`-w` 控制 master weights 的解析。

用法提示里写得很清楚：

[train_gpt2.cu:1402](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1402) —— `-w <int>    keep f32 copy of weights for the optimizer? (default: 1)`。

**(7) checkpoint 的存留。** master weights 会被持久化到 state 文件里，且头部记录了开关状态，以便恢复时对齐：

[train_gpt2.cu:1219](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1219) —— 写 state 时头部第 4 项记录 `use_master_weights`。

[train_gpt2.cu:1235-1236](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1235-L1236) —— 若开启，则把 `master_weights`（FP32）写盘。

#### 4.2.4 代码实践

**实践目标**：观察「开/关 master weights」对显存的影响，并理解关掉它的代价。

**操作步骤**：

1. 用默认配置编译运行：`make train_gpt2cu && ./train_gpt2cu`，记录启动日志里这两行：
   - `allocating ... MiB for AdamW optimizer state m`
   - `allocating ... MiB for master copy of params`（见 [train_gpt2.cu:398-407](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L398-L407)）
2. 关掉 master weights 再跑：`./train_gpt2cu -w 0`。
3. 观察启动表格中 `use_master_weights` 行从 `enabled` 变 `disabled`（见 [train_gpt2.cu:1547](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1547)），且不再打印「master copy of params」那一行分配。

**需要观察的现象**：

- 开启时：master weights 占 `num_params * 4` 字节（124M 模型约 474 MiB），与 `m`、`v` 各占同样大小。三者合计约 1.4 GiB 的优化器状态。
- 关闭时（`-w 0`）：省掉这 474 MiB，但优化器退化为在 bf16 上更新（[adamw.cuh:38](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L38) 的三元表达式走 `else` 分支），小更新会被舍掉。

**预期结果**：在长训练里，`-w 0` 虽然省显存，但 loss 曲线通常比 `-w 1` 更差、更易发散，因为 BF16 无法可靠表示微小更新。这就是为什么默认是 `1`（[train_gpt2.cu:347](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L347) 注释称其为「safe default」）。

**待本地验证**：显存具体 MiB 数值与 loss 走向请以本地 GPU 实测为准；本实践更重要的收获是理解「开关一改，显存差一份、精度差一截」的因果关系。

#### 4.2.5 小练习与答案

**练习 1**：假如某步的更新量 \(\Delta\theta = 3\times 10^{-6}\)，权重 \(\theta=0.4\)。若不用 master weights、直接在 BF16 上更新，这一步会发生什么？

**参考答案**：BF16 在 0.4 附近的可表示间隔约为 \(0.4 \times 2^{-7} \approx 3.1\times 10^{-3}\)，远大于 \(\Delta\theta\)。所以 \(0.4 + 3\times 10^{-6}\) 在 BF16 里会舍入回 \(0.4\)，这一步更新被完全吞掉。若用 FP32 master 累加，\(\Delta\theta\) 会被忠实记录在 master 中，等多次累加到 \(10^{-3}\) 量级后再通过随机舍入真正反映到 bf16 工作副本上。

**练习 2**：`stochastic_rounding` 的 FP16 重载为什么「不算数」？

**参考答案**：见 [cuda_utils.cuh:279-281](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L279-L281)，FP16 版只做了 `*out = (float)in;`（普通赋值），并没有像 BF16 版那样用随机阈值做无偏舍入。这说明 FP16 路径尚未完工，也是 llm.c 不推荐 FP16 的又一处证据。

---

### 4.3 TF32：FP32 数据的加速计算模式

#### 4.3.1 概念说明

TF32 是本讲最容易误解的概念。**TF32 不是一种存储格式**，你不会在显存里存一个「TF32 张量」；它是 NVIDIA Ampere（compute capability 8.x）及以上 GPU 的 tensor core 提供的一种**矩阵乘法的计算模式**：输入的 FP32 数据会被硬件当成「1+8+10 = 19 位」的截断格式送进 tensor core 做乘加，但**累加仍在 FP32 精度下进行**。

要真正理解它，必须区分**两类精度**：

- **数据精度（data type）**：张量在显存里用什么格式存。对应 cuBLAS 的矩阵 layout 类型，llm.c 里叫 `CUBLAS_LOWP`，随 `PRECISION` 切换（`CUDA_R_32F` / `CUDA_R_16F` / `CUDA_R_16BF`）。
- **计算精度（compute type）**：矩阵乘法内部用什么精度**累加**部分和。对应 cuBLAS 的 `cublasComputeType_t`，llm.c 里叫 `cublas_compute`。

混合精度之所以能工作，关键就是：**哪怕数据是 BF16/FP16，cuBLAS 默认仍用 FP32 来累加部分和**（`cublas_compute` 默认 `CUBLAS_COMPUTE_32F`）。BF16/FP16 尾数太短，若也用它们累加几百几千个乘积，舍入误差会迅速放大；用 FP32 累加则把误差压到很小。

TF32 是这个框架下的一个**特例**：当数据本身就是 FP32 时，也可以让 tensor core 用「截断的 FP32（即 TF32，10 位尾数）」来乘、仍用 FP32 来累加，从而在几乎不损失最终精度的情况下大幅提速。它等价于 PyTorch 里的 `torch.set_float32_matmul_precision('high')`。代码里也写了这条对照：

> `// TF32 precision is equivalent to torch.set_float32_matmul_precision('high')`

要点小结：

| 情形 | 数据精度 | 计算精度 | 是否用 TF32 |
|------|----------|----------|-------------|
| BF16 训练（默认） | BF16 | FP32 | 否（TF32 与 BF16/FP16 无关） |
| FP16 训练 | FP16 | FP32 | 否 |
| FP32 + Ampere+ + `-f 1` | FP32 | **TF32**（19 位乘、FP32 累加） | 是 |
| FP32 + 老 GPU 或 `-f 0` | FP32 | FP32 | 否 |

#### 4.3.2 核心流程

TF32 开关的传导链很短，全部发生在 `common_start` 里：

```text
common_start(override_enable_tf32):
  enable_tf32 = (PRECISION_MODE == FP32)         ← 必须是 FP32 数据
             && (deviceProp.major >= 8)          ← 必须 Ampere+（SM 8.x 及以上）
             && (override_enable_tf32)           ← 用户用 -f 没关掉
  cublas_compute = enable_tf32 ? CUBLAS_COMPUTE_32F_FAST_TF32
                               : CUBLAS_COMPUTE_32F
            ↓
  所有 matmul 的 cublasLtMatmulDescCreate(... cublas_compute ...) 都跟着用这个计算类型
```

注意三个条件是**与**的关系：只要任一不满足（比如你跑 BF16、或 GPU 是老架构、或手动 `-f 0`），就走纯 FP32 计算。BF16/FP16 训练时 TF32 永远不介入——这正是很多人误以为「TF32 会影响 BF16 训练」时要澄清的点。

#### 4.3.3 源码精读

**(1) cublas_compute 的默认值与定义位置。** 计算类型是一个全局变量，默认纯 FP32：

[cublas_common.h:30](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L30) —— `cublasComputeType_t cublas_compute = CUBLAS_COMPUTE_32F;`，默认 FP32 累加。

顺便看一眼数据类型宏，理解「数据精度」如何随 `PRECISION` 切换：

[cublas_common.h:16-22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cublas_common.h#L16-L22) —— `CUBLAS_LOWP` 三分支对应 `CUDA_R_32F` / `CUDA_R_16F` / `CUDA_R_16BF`，与 `floatX` 同源。

**(2) common_start 里设置 TF32。** 这是开关的真正落点：

[train_gpt2.cu:1190-1192](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1190-L1192) —— 注释点明与 PyTorch 的对应关系；第 1191 行用三条件与运算决定 `enable_tf32`；第 1192 行据此把 `cublas_compute` 设为 `CUBLAS_COMPUTE_32F_FAST_TF32` 或 `CUBLAS_COMPUTE_32F`。

**(3) cublas_compute 如何被 matmul 消费。** 这个全局变量最终喂给 cuBLASLt 的 matmul 描述符：

[matmul.cuh:126](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L126) —— `cublasLtMatmulDescCreate(&operationDesc, cublas_compute, CUDA_R_32F);`。第一个类型参数（`cublas_compute`）是「计算/累加类型」，第二个（`CUDA_R_32F`）是 scale 类型。无论 `cublas_compute` 是 TF32 还是 FP32，scale 类型恒为 FP32。

对照看数据 layout 用的是 `CUBLAS_LOWP`（随精度变），例如 [matmul.cuh:143](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/matmul.cuh#L143) —— 这就把「数据精度」与「计算精度」两个参数清楚地分开了。

**(4) 启动时的可观测信号。** TF32 是否生效，会反映在启动表格的 `precision` 行：

[train_gpt2.cu:1551-1553](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1551-L1553) —— FP32 数据下，若 `cublas_compute == CUBLAS_COMPUTE_32F_FAST_TF32` 打印 `TF32`，否则打印 `FP32`。BF16/FP16 则直接打印对应名字，TF32 分支根本不进入。

**(5) `-f` 命令行开关。**

[train_gpt2.cu:1401](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1401) —— `-f <int>    enable_tf32 override (default: 1, set to 0 to disable tf32)`。

#### 4.3.4 代码实践

**实践目标**：在 FP32 模式下亲手开关 TF32，观察启动打印与（可能的）速度差异。

**操作步骤**：

1. 用 FP32 编译：`make clean && make train_gpt2cu PRECISION=FP32`（记得准备 fp32 权重并用 `-e` 指定）。
2. 默认开 TF32 运行：`./train_gpt2cu`，观察启动表格 `precision` 行打印 `TF32`（前提是你的 GPU 是 Ampere+，即 `deviceProp.major >= 8`）。
3. 关掉 TF32 再跑：`./train_gpt2cu -f 0`，观察 `precision` 行变成 `FP32`。
4. （对照）切回 BF16：`make clean && make train_gpt2cu`，运行 `./train_gpt2cu`，观察 `precision` 行打印 `BF16`——此时 `-f` 无论取何值都不影响，因为 `enable_tf32` 的第一个条件（`PRECISION_MODE == FP32`）就不满足。

**需要观察的现象**：

- FP32 + `-f 1`（TF32）的每步耗时通常**短于** FP32 + `-f 0`（纯 FP32），因为前者用上了 tensor core。
- 两种模式下的 loss 曲线应当非常接近（TF32 仅尾数略短，累加仍 FP32，对最终精度影响很小）。
- BF16 模式下，`-f` 的取值对训练完全无影响。

**预期结果**：在 Ampere/Hopper GPU 上，FP32 训练开 TF32 能拿到可观的加速且几乎不损精度；这正是它作为默认值（`-f` 默认 1）的原因。如果你用的是 Volta 或更老架构（major < 8），则即使 `-f 1` 也会因为第二个条件不满足而退化为纯 FP32。

**待本地验证**：具体加速比依赖你的 GPU 型号与模型规模，请以本地实测为准。

#### 4.3.5 小练习与答案

**练习 1**：你在 BF16 模式下用 `./train_gpt2cu -f 0` 想关掉 TF32，会有什么效果？

**参考答案**：没有任何效果。`enable_tf32` 的第一个条件 `PRECISION_MODE == PRECISION_FP32` 在 BF16 模式下为假，三者取与后必为假，`cublas_compute` 始终是 `CUBLAS_COMPUTE_32F`（FP32 累加）。`-f` 只在 FP32 数据模式下才有意义。

**练习 2**：为什么 BF16 数据下 cuBLAS 默认仍用 FP32 累加（而不是用 BF16 累加）？

**参考答案**：单个 BF16 乘积的尾数只有 7 位，若把成百上千个这样的乘积直接用 BF16 累加，舍入误差会迅速累积、吞噬有效信息。用 FP32（24 位尾数）累加可以把累加误差压到很小，这正是「混合精度」能稳态工作的前提。`cublas_compute` 默认 `CUBLAS_COMPUTE_32F` 体现的就是这一点。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「精度决策清单」的综合练习。

**任务背景**：假设你要在一台 8 卡 H100 节点上训练 GPT-2 124M，显存充裕但追求训练速度；另假设你还要在一台老 Titan V（Volta，SM 7.0）上做一次「正确性回归」对照。

**请你完成**：

1. **选定 H100 上的配置**：写出合适的 `make` 命令与运行参数，使得
   - 用 BF16 数据精度（前向/反向快、省显存）；
   - 保留 FP32 master weights（保证优化器精度）；
   - 在这个配置下，TF32 是否启用？为什么？（提示：回到 [train_gpt2.cu:1191](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1191) 的第一个条件。）

2. **选定 Titan V 上的回归配置**：为了让 `test_gpt2cu` 的数值对照最严格，应当用什么 `PRECISION`？此时 TF32 会启用吗（注意 `deviceProp.major >= 8` 这个条件）？

3. **画出数据流**：在一张图/一段文字里标出，一次「前向→反向→更新」中，`params_memory`、`grads_memory`、`m_memory`/`v_memory`、`master_weights` 各自的精度，以及它们之间的读写方向（前向读谁、更新写谁、随机舍入从谁到谁）。

**参考要点**：

- H100 配置：`make train_gpt2cu`（默认 BF16）+ `./train_gpt2cu`（`-w` 默认 1）。TF32 **不启用**——因为数据是 BF16，`PRECISION_MODE != FP32`，所以 TF32 分支不进入；H100 上 BF16 本就走 tensor core，已经够快。
- Titan V 回归：`make test_gpt2cu PRECISION=FP32`。TF32 **不启用**——尽管 `PRECISION_MODE == FP32`，但 Volta 的 `deviceProp.major = 7 < 8`，硬件不支持 TF32，自动退化为纯 FP32（这反而是回归测试想要的「最严格」基准）。
- 数据流：前向/反向读写 `params_memory`(bf16) 与 `grads_memory`(bf16)；`gpt2_update` 读 `m_memory`/`v_memory`(fp32) 与 `master_weights`(fp32)，在 fp32 上算出新参数，再 `stochastic_rounding` 写回 `params_memory`(bf16)，并把 fp32 新值回写 `master_weights`。

## 6. 本讲小结

- llm.c 把精度做成**编译期选项**：`Makefile` 的 `PRECISION`（默认 `BF16`）→ `-DENABLE_*` 宏（`PFLAGS`）→ `cuda_common.h` 的 `floatX` 类型别名；且 `PFLAGS` 只作用于主线 `train_gpt2cu`，不影响恒为 fp32 的 legacy 版。
- 三种格式各有定位：**FP32** 最准最慢、**BF16** 是混合精度主力（动态范围与 FP32 相同、无需 gradient scaler）、**FP16** 因缺少 gradient scaler 而基本不可用。
- **master weights** 解决「BF16 装不下微小更新」的问题：前向/反向用 BF16，优化器在 FP32 master 上更新，再用**无偏的随机舍入**写回 BF16 工作副本，由 `-w` 控制（默认开，是「safe default」）。
- 必须区分**数据精度**（`CUBLAS_LOWP`，随 `PRECISION` 变）与**计算精度**（`cublas_compute`，默认 FP32 累加）；混合精度能稳定工作的关键就是低精度数据 + FP32 累加。
- **TF32** 只是 FP32 数据在 Ampere+ tensor core 上的一种「19 位乘、FP32 累加」的**加速计算模式**，不是存储格式；由 `PRECISION_MODE==FP32 && major>=8 && -f` 三条件与决定，BF16/FP16 训练时永远不介入。
- 三个开关的运行时可观测信号都汇总在启动表格：`precision` 行（FP32/TF32/FP16/BF16）与 `use_master_weights` 行。

## 7. 下一步学习建议

- 想看 master weights 与随机舍入如何与**多卡 ZeRO 分片**协作（master 也按卡分片），请继续学 **u6-l4（多 GPU 训练：ZeRO 分片与 NCCL）**。
- 想了解 recompute（反向重算换显存）与融合算子、global_norm 梯度裁剪（本讲多次提到的 `grad_scale` 就是它的产物），请学 **u6-l2（重计算、融合算子与 global norm）**。
- 想从 kernel 层面看 master weights 的 fp32↔bf16 转换与 `stochastic_rounding` 的位操作细节，可复习 **u5-l4（各层 CUDA kernel）**。
- 建议动手把本讲「综合实践」的配置在真实 GPU 上跑一遍，对照启动表格逐行验证你对三个开关的理解。
