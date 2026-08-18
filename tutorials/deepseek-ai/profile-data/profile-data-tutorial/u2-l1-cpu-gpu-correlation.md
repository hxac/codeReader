# u2-l1 CPU 与 GPU 的关联：cuda_runtime、External id 与 ac2g

## 1. 本讲目标

u1-l4 讲清了「一条事件长什么样」，并留下一个悬念：CPU 上的算子调用和 GPU 上的内核执行是**异步**的，同一次内核启动会在轨迹里留下**两段记录**——CPU 侧一条 `cuda_runtime`、GPU 侧一条 `kernel`。本讲就要把这两段记录（以及再往上的 CPU 算子）正式**接起来**。读完本讲，你应该能够：

1. 说出一次内核启动在轨迹中的完整三层记录：`cpu_op`（算子）→ `cuda_runtime`/`cuda_driver`（CUDA API 调用）→ `kernel`/`gpu_memcpy`/`gpu_memset`（GPU 上的实际执行）。
2. 用 `args` 里的 **`correlation`** 字段把 GPU 内核一一反查回 CPU 侧的那条 API 调用，并用数字对账验证这种配对是严格一对一的。
3. 解释 **`ac2g` 流事件**（`ph:"s"` / `ph:"f"`，按相同 `id` 配对）就是 chrome://tracing 时间线上 CPU→GPU 箭头的数据来源，并知道哪些 s/f 是真正成对的箭头、哪些是不成对的「孤儿」。
4. 辨析三个容易混淆的编号：`Ev Idx`、CPU 侧 `External id`、CUDA 侧 `External id`——并用实证说明**在本仓库数据中**该用哪种方法把 GPU 内核追回 CPU 算子（区间嵌套法），以及为什么「拿 External id 直接 join」在这里行不通。
5. 量化**提交→执行延迟** \( \Delta = ts_{kernel} - ts_{runtime} \)，并用它解释训练与预填充两种场景下 CPU/GPU 的不同节奏。

本讲所有计数（如 train.json 里 983 个设备侧事件、983 条启动类 API、983 根 CPU→GPU 箭头）都经过作者对文件的逐类文本统计核实，读者运行讲义脚本应得到完全相同的数字。

## 2. 前置知识

- **回顾 u1-l4**：事件八字段 `ph/cat/name/pid/tid/ts/dur/args`；`X` 完成事件可按区间 \([ts, ts+dur]\) 在同一 `pid/tid` 上嵌套；`M` 元数据把 pid 1166 命名为 CPU、pid 0 命名为 GPU 0、tid 7/23/27 命名为 stream；train.json 共 14240 条事件，`ts` 是 Unix 纪元微秒。
- **CUDA 异步执行模型**（本讲的地基）：CPU（host）调用 `cudaLaunchKernel` 这类 API 时，只是把内核**提交**进某个流（stream，GPU 上的任务队列），函数立刻返回，CPU 继续往下跑；GPU 排到队头才**真正执行**。所以「CPU 何时提交」和「GPU 何时执行」是两个时间点，中间的差就是排队延迟。
- **外键 / join 的直觉**：把 `traceEvents` 想成一张大表，「关联字段」就是表与表之间的外键——`correlation` 是 CUDA API 记录与 GPU 活动记录之间的外键，`ac2g` 的 `id` 是箭头两端的外键。做性能分析时，我们不断在做的事就是**沿着外键把散落的记录拼回一条因果链**。
- **CUPTI 是谁**：PyTorch Profiler 底层用 CUPTI（CUDA 的采集库）拦截所有 CUDA API 调用与 GPU 活动。CUPTI 给每次 API 调用分配一个唯一的 `correlation` 编号，GPU 上执行对应活动时把这个编号带回来——这就是配对得以成立的机制。
- **简单区间运算**：判断事件 A 是否包含事件 B，就是检查 \( ts_A \le ts_B \) 且 \( ts_B + dur_B \le ts_A + dur_A \)（同一 `pid/tid` 上才有嵌套意义）。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json)（97654 行，多行排版） | 主精读对象：所有配对样本与全量对账都出自它 |
| [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json)（570649 行） | 对照对象：验证同一套关联机制，并观察「深排队」形态（CPU pid 是 1097，与 train 的 1166 不同） |

decode.json 是单行压缩文件无法按行引用，本讲不引用其行号，但讲义脚本的思路对它同样适用（用 `json.load` 解析后一切字段同理）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. `cuda_runtime` 事件与 `correlation` 字段：一次内核启动的两段记录如何配对
2. `External id` 与 `Ev Idx` 的链接作用：三个编号的辨析与追回 CPU 算子的正确方法
3. `ac2g` 流事件：时间线上那根箭头的数据来源

### 4.1 cuda_runtime 事件与 correlation 字段

#### 4.1.1 概念说明

`cat:"cuda_runtime"` 与 `cat:"cuda_driver"` 记录的是 **CPU 侧**对 CUDA 库函数的每一次调用（前者是运行时 API 如 `cudaLaunchKernel`，后者是驱动 API 如 `cuLaunchKernel`）。它们本身不代表 GPU 干活，只代表「CPU 在这一刻提交/查询/同步了某件事」。

其中只有一小部分是**启动类** API——真正会往 GPU 流里放入工作的调用；其余大量是查询与属性设置类（如 `cudaEventQuery`、`cudaFuncSetAttribute`），它们没有对应的 GPU 活动。

CUPTI 给每次 API 调用分配唯一编号 `correlation`（写在 `args` 里），GPU 侧的 `kernel`/`gpu_memcpy`/`gpu_memset` 事件会把同一个编号带回来。于是：

\[ \text{cuda\_runtime}(\text{correlation}=c) \;\longleftrightarrow\; \text{kernel}(\text{args.correlation}=c) \]

这是一对一的外键关系，不是模糊匹配。

#### 4.1.2 核心流程

把 train.json 的 CPU 侧 API 和 GPU 侧活动各自数一遍，可以**完全对账**（数字均经作者文本统计核实）：

| CPU 侧启动类 API | 数量 | 对应 GPU 侧活动 | 数量 |
|---|---|---|---|
| `cudaLaunchKernel`（cuda_runtime） | 727 | | |
| `cudaLaunchKernelExC`（cuda_runtime，带扩展参数的启动变体） | 196 | | |
| `cuLaunchKernel`（cuda_driver） | 40 | | |
| `cudaMemcpyAsync`（cuda_runtime） | 8 | `gpu_memcpy` | 8 |
| `cudaMemsetAsync`（cuda_runtime） | 12 | `gpu_memset` | 12 |
| **合计** | **983** | `kernel` 963 + 8 + 12 | **983** |

一边 983 条提交、另一边 983 个设备活动，数量严格相等；剩余的 CPU 侧 API（train.json 共 3853 条 `cuda_runtime` + 40 条 `cuda_driver`，其中 `cudaEventQuery` 就有 2241 条）不产生 GPU 活动，也就没有配对对象。

反查的标准流程：

```text
选中一个 GPU kernel 事件 e
  ├─ 1. c = e["args"]["correlation"]
  ├─ 2. 在 cpu 侧扫描 cat ∈ {cuda_runtime, cuda_driver} 的事件
  │       找 args["correlation"] == c 的那条 → 得到 API 名与提交时刻
  ├─ 3. lag = e["ts"] - api["ts"]   （提交→执行延迟，微秒）
  └─ 4. 想再往上找发起这次调用的算子 → 进入 4.2 的区间嵌套法
```

#### 4.1.3 源码精读

**黄金样本：一组完整的「两段记录」加箭头**。train.json 中这四条事件在文件里紧挨着出现，是本讲最重要的 30 行：

[train.json:L31392-L31422](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31392-L31422) —— 第一条（L31392）是 GPU 侧 `kernel` 事件：`layer_norm::ln_fwd_kernel`（层归一化前向内核），位于 `pid 0 / tid 7`（GPU 0 的 stream 7），区间 \([1740461679471821,\ 1740461679471933]\)，`args` 里 `correlation: 44633`，还带 `stream: 7` 与硬件开销信息（每线程寄存器 168、grid \([396,1,1]\)、block \([128,1,1]\)、估计占用率 19%）。第四条（L31412）是 CPU 侧配对事件：`cuda_runtime` 类的 `cudaLaunchKernel`，位于 `pid 1166 / tid 1166`（CPU 主线程），区间 \([1740461679471809,\ 1740461679471820]\)，`args` 里同样是 `correlation: 44633`（还有 `cbid: 211`，是 CUDA 给这个 API 的编号）。两条靠同一个 44633 配成一对。夹在中间的两条 `cat:"ac2g"` 事件（L31408 的 `ph:"f"` 与 L31420 的 `ph:"s"`）就是连接它们的箭头，留到 4.3 细讲。

时间关系值得细看：CPU 在 …471809 开始提交、11 µs 后返回；GPU 在 …471821 开始执行，恰好晚 12 µs——提交完成（471820）后 1 µs 内 GPU 就接手了，说明此刻 stream 7 是空闲的。

**第二个样本：走 `cudaLaunchKernelExC` 的 DeepEP 通信内核**。

[train.json:L31292-L31322](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31292-L31322) —— GPU 侧 `dpsk::ep::internode::cached_notify`（DeepEP 节点间通信的通知内核，stream 27，dur 1008 µs）与 CPU 侧 `cudaLaunchKernelExC`（`cbid: 430`）靠 `correlation: 44493` 配对。紧随其后：

[train.json:L31324-L31354](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31354) —— `dpsk::ep::internode::dispatch` 内核（token 发往各专家的 all-to-all 前半程，stream 27，dur 3320 µs，grid 只有 \([20,1,1]\)、占用率 4%——它是通信受限内核，不是算力受限的）配 `cudaLaunchKernelExC`，`correlation: 44508`。注意它的提交→执行延迟是 \( 1740461679472450 - 1740461679471452 = 998 \) µs：不是 GPU 没空，而是**同一根 stream 27 上前一个 `cached_notify` 还在跑**（它占了 …471440 起 1008 µs），流是串行队列，排队是正常现象。

**第三个样本：换一份轨迹同样成立，但形态完全不同**。

[prefill.json:L205245-L205272](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L205245-L205272) —— GPU 侧 `_layer_norm_kernel`（stream 7，dur 21 µs，grid \([16384,1,1]\)、占用率 100%，与 train 的同名内核相比是截然不同的吞吐形态）配 CPU 侧 `cat:"cuda_driver"` 的 `cuLaunchKernel`（`cbid: 307`），`correlation: 146569`。这里的 CPU 进程是 `pid 1097 / tid 1097`（预填充采集的进程号与 train 的 1166 不同，进程号只在本份轨迹内有效）。延迟：\( 1740462780110788 - 1740462780094839 = 15949 \) µs ≈ **16 毫秒**——CPU 已经往前跑了 16 ms，GPU 队列里积压着大量待执行内核。这是吞吐型推理（大批量预填充）的典型形态：CPU 提交远快于 GPU 消化，与 train 单步 1F1B 里「提交后微秒级就被接手」形成鲜明对比。

prefill.json 的对账同样严格成立（数字经作者核实）：`kernel` 4111 + `gpu_memcpy` 37 + `gpu_memset` 4 = **4152** 个设备侧活动，CPU 侧 `cuda_runtime` 25606 条中启动类的恰好也是 4152 条，一一配对。

#### 4.1.4 代码实践

**实践目标**：亲手数出「提交数 = 执行数」，验证 correlation 配对是一对一。

**操作步骤**（示例代码）：

```python
# audit_launches.py —— 示例代码
import json
from collections import Counter

evts = json.load(open("train.json", encoding="utf-8"))["traceEvents"]

LAUNCH = {"cudaLaunchKernel", "cudaLaunchKernelExC", "cuLaunchKernel",
          "cudaMemcpyAsync", "cudaMemsetAsync"}
DEV = {"kernel", "gpu_memcpy", "gpu_memset"}

launches = [e for e in evts if e.get("cat") in ("cuda_runtime", "cuda_driver")
            and e["name"] in LAUNCH]
devices  = [e for e in evts if e.get("cat") in DEV]

print("启动类 API 按名称:", dict(Counter(e["name"] for e in launches)))
print("设备侧活动按类别:", dict(Counter(e["cat"] for e in devices)))
print("提交数 =", len(launches), " 执行数 =", len(devices))

# 每个 correlation 在两侧各出现几次？（一对一 ⟺ 每个值都恰好 1:1）
c_cpu = Counter(e["args"]["correlation"] for e in launches)
c_gpu = Counter(e["args"]["correlation"] for e in devices)
print("correlation 在 CPU 侧最大重复次数:", max(c_cpu.values()))
print("correlation 在 GPU 侧最大重复次数:", max(c_gpu.values()))
print("只在 CPU 出现 / 只在 GPU 出现的 correlation 数:",
      len(set(c_cpu) - set(c_gpu)), len(set(c_gpu) - set(c_cpu)))
```

**需要观察的现象**：启动 API 名称榜单、两侧计数是否相等、correlation 是否严格一对一。

**预期结果**（train.json，数字已由作者核实）：启动类 API 为 `cudaLaunchKernel 727 + cudaLaunchKernelExC 196 + cudaMemcpyAsync 8 + cudaMemsetAsync 12 + cuLaunchKernel 40`；设备侧为 `kernel 963 + gpu_memcpy 8 + gpu_memset 12`；两侧都是 983；correlation 在两侧最大重复次数都是 1；只在单侧出现的 correlation 数为 0、0。换成 prefill.json 跑一遍，两侧都应是 4152（**prefill 的名称分布留给你运行填写，待本地验证**）。decode.json 同理可跑（单行格式不影响 `json.load`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 train.json 有 3853 条 `cuda_runtime`，却只有 983 个设备侧活动？多出来的记在哪里？
**答案**：大多数 CUDA API 调用不往流里放工作——例如 `cudaEventQuery`（轮询事件状态，train.json 中有 2241 条，是 DeepEP 等待逻辑里的忙轮询）、`cudaFuncSetAttribute`、`cudaStreamIsCapturing` 等。它们只有 CPU 侧记录、没有 `correlation` 配对的 GPU 活动。

**练习 2**：一个 GPU 内核的 `correlation` 是 44508，它的提交→执行延迟是 998 µs。延迟大一定说明「GPU 很忙、整个 GPU 被占满」吗？
**答案**：不一定。延迟只说明**这条流**当时还没轮到它。4.1.3 的第二个样本里，stream 27 上前一个 `cached_notify` 内核要跑 1008 µs，`dispatch` 只是在同一根流里排队；与此同时 stream 7（计算流）可以在旁边并行干活。判断「GPU 整体忙不忙」要看所有流的并集区间，那是 u3-l2 重叠率分析的主题。

**练习 3**：`cudaLaunchKernel` 与 `cudaLaunchKernelExC` 有何区别？为什么两份轨迹里都大量出现后者？
**答案**：`ExC` 是带扩展启动配置的变体（如按属性启动、设置集群/缓存属性等）。DeepEP、DeepGEMM 这类手写内核常用 `cudaLaunchKernelExC`/驱动级 `cuLaunchKernel` 启动以满足共享内存、集群等特殊要求；PyTorch 普通算子则多走 `cudaLaunchKernel`。对分析者而言两者地位相同：都是「启动类 API」，都靠 `correlation` 配对。

### 4.2 External id 与 Ev Idx 的链接作用

#### 4.2.1 概念说明

轨迹里一共会出现三种编号，初学者极易混淆：

| 编号 | 出现在哪 | 作用 | 本仓库实测形态 |
|---|---|---|---|
| `Ev Idx` | `cpu_op` / `user_annotation` 的 `args` | CPU 侧事件的**顺序号**（第几条被记录） | 从 0 起递增，如 5633 号事件带 `Ev Idx: 0` |
| `External id`（CPU 侧） | `cpu_op` / `user_annotation` 的 `args` | PyTorch 给每个算子/注解分配的编号 | train.json 全部 ≤ 4 位数（0 个 5 位数以上） |
| `External id`（CUDA 侧） | `cuda_runtime` / `cuda_driver` / `kernel` / `gpu_memcpy` / `gpu_memset` 的 `args` | 设计意图是跨层关联 CPU 上下文 | train.json 全部恰为 5 位数，且**实测与 `correlation` 数值相等**（作者抽查 train 8 对、prefill 4 对，无一例外） |

这里必须给出一个**实证修正**——它和很多教程（包括 u1-l4 结尾的预告）的直觉相反：在本仓库的数据里，**不能**拿 `External id` 把 GPU 内核直接 join 到 CPU 算子。两条证据：

1. train.json 中 CPU 侧 `External id` 全部 ≤ 9999，而 CUDA 侧 `External id` 全部 ≥ 10000（4876 个无一例外），两个编号空间**完全不相交**，join 结果必为空。
2. CUDA 侧 `External id` 与 `correlation` 逐对相等，也就是说它没有携带任何 `correlation` 之外的信息；逐个检索（如 44633、prefill 的 134683、134948）也确认这些值只出现在 kernel 与其 API 两条记录上，从不出现在任何 `cpu_op` 上。

所以本讲教一个**不依赖版本行为、只靠几何关系**的通用方法——**同线程区间嵌套法**：CPU 侧 API 调用一定发生在某个正在执行的算子作用域内，因此在**同一 `pid/tid`** 上，找区间把这条 API 记录完整包住的最内层 `cpu_op`，就是发起调用的算子；继续向外找，还能得到注解链（如 `attn(F)` → `1F1B` → `ProfilerStep#1`）。

\[ \text{父算子判定}:\quad ts_{op} \le ts_{api}\ \wedge\ ts_{api} + dur_{api} \le ts_{op} + dur_{op}\quad(\text{同一 } pid/tid,\ \text{取最内层}) \]

#### 4.2.2 核心流程

把一条 GPU 内核追到人类可读的调用上下文，完整链路是：

```text
GPU kernel (args.correlation = c)
   │  ① 按 correlation 反查
CPU cuda_runtime/cuda_driver (correlation = c, ts_api, dur_api, pid, tid)
   │  ② 在同一 pid/tid 的 cpu_op / user_annotation 里
   │     找区间包含 [ts_api, ts_api+dur_api] 的最内层事件
最内层 cpu_op（发起调用的算子）
   │  ③ 继续向外逐层扩张区间
外层注解链：attn(F) → 1F1B → ProfilerStep#1
```

得到的完整因果链读作：「`ProfilerStep#1` 这一步的 `1F1B` 调度里，`attn(F)` 前向注意力阶段中的 `DropoutAddLayerNormFn` 算子，通过 `cudaLaunchKernel` 提交了 GPU 0 stream 7 上的 `ln_fwd_kernel`」。

#### 4.2.3 源码精读

先看 CPU 侧两种编号的原貌：

[train.json:L72-L98](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L72-L98) —— 前三条 `cpu_op`：`autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward` 带 `External id: 5633, Ev Idx: 0`，其后 `BackwardWithAuxiliaryLossBackward` 是 5634/1、`aten::ones` 是 5635/2。`Ev Idx` 沿文件记录顺序 0、1、2 递增；`External id` 是另一套算子编号（反向算子还带 `Fwd thread id`、`Sequence number` 用于回指前向，u1-l4 已见过）。注意这里的 `External id` 都是 4 位数——与 CUDA 侧的 5 位数空间不相交。

再看黄金样本 44633 的完整落点（三段记录的行号）：

[train.json:L14675-L14701](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14675-L14701) —— `user_annotation` **`attn(F)`**：区间 \([1740461679471616,\ 1740461679474898]\)（dur 3282 µs），`External id: 4646`；其内第 4 层是 `cpu_op` **`DropoutAddLayerNormFn`**：区间 \([1740461679471756,\ 1740461679471880]\)（dur 124 µs），`External id: 4649`。

[train.json:L31412-L31418](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31412-L31418) —— `cudaLaunchKernel`：区间 \([1740461679471809,\ 1740461679471820]\)，落在 `DropoutAddLayerNormFn` 区间之内（471756 ≤ 471809 且 471820 ≤ 471880）✓。

[train.json:L31392-L31398](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31392-L31398) —— GPU 侧 `ln_fwd_kernel`：`correlation: 44633`，stream 7。

三者拼成（时间单位 µs，均经作者核对）：

| 层 | 事件 | 轨道 | 区间 |
|---|---|---|---|
| 注解 | `ProfilerStep#1` | CPU 1166/1166 | [4710813, 4823015] |
| 注解 | `1F1B` | CPU 1166/1166 | [4710833, 4823001] |
| 注解 | `attn(F)` | CPU 1166/1166 | [471616, 474898] |
| 算子 | `DropoutAddLayerNormFn` | CPU 1166/1166 | [471756, 471880] |
| API | `cudaLaunchKernel`（correlation 44633） | CPU 1166/1166 | [471809, 471820] |
| 内核 | `layer_norm::ln_fwd_kernel` | GPU 0 / stream 7 | [471821, 471933] |

语义也互相印证：提交它的算子叫 DropoutAddLayerNorm**Fn**，执行的内核是 layer_norm 的 **fwd**——名字对得上，嵌套法找回的父算子是可信的。（`ProfilerStep#1`/`1F1B` 两层的原始记录见 [train.json:L14416-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14436)，u1-l4 已读过，u2-l2 将专门展开。）

对照一份 CPU 侧编号更大的轨迹，确认「空间不相交」不是巧合的数值错觉：

[prefill.json:L175934-L175939](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175934-L175939) —— prefill 的 `dpsk::ep::internode::dispatch` 内核（注意它在 prefill 里跑在 **stream 16**，而 train 的通信流是 stream 27——流编号是每次采集自行分配的，不能跨文件比较），`External id` 与 `correlation` 同为 134683。检索整个 prefill.json，134683 只出现在 kernel 与其 `cuda_runtime` 两条记录里，任何 `cpu_op` 都不持有它——尽管 prefill 的 CPU 侧编号已增长到 5 位数区间、与 CUDA 侧数值范围重叠，join 依然落空。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：在 train.json 中任选一个 GPU kernel，读取其 `args.correlation`，编写脚本在 CPU 侧找到同 `correlation` 的 API 事件，再用区间嵌套法找出外层 `cpu_op` 算子与注解链，打印「GPU 内核 ← CUDA API ← CPU 算子」整条链路。

**操作步骤**（示例代码）：

```python
# trace_link.py —— 示例代码
import json

def load(path):
    return json.load(open(path, encoding="utf-8"))["traceEvents"]

def find_chain(evts, corr):
    # ① GPU 侧：按 correlation 找设备事件
    gpu = next(e for e in evts
               if e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset")
               and e.get("args", {}).get("correlation") == corr)
    # ② CPU 侧：同 correlation 的 API 调用
    api = next(e for e in evts
               if e.get("cat") in ("cuda_runtime", "cuda_driver")
               and e.get("args", {}).get("correlation") == corr)
    # ③ 同 pid/tid 上，找区间完整包住 API 记录的所有 cpu_op / user_annotation
    s, t = api["ts"], api["ts"] + api["dur"]
    elders = [e for e in evts
              if e.get("ph") == "X" and e.get("cat") in ("cpu_op", "user_annotation")
              and e["pid"] == api["pid"] and e["tid"] == api["tid"]
              and e["ts"] <= s and t <= e["ts"] + e["dur"]]
    elders.sort(key=lambda e: e["ts"])          # 外 → 内
    return gpu, api, elders

if __name__ == "__main__":
    evts = load("train.json")
    for corr in (44633, 44508):                  # ln_fwd 样本 / DeepEP dispatch 样本
        gpu, api, elders = find_chain(evts, corr)
        print(f"=== correlation {corr} ===")
        print(f"[GPU ] {gpu['cat']:10} {gpu['name'][:60]}")
        print(f"      ts={gpu['ts']} dur={gpu['dur']} stream={gpu['args']['stream']}")
        print(f"[CPU ] {api['cat']:10} {api['name']}  ts={api['ts']} dur={api['dur']}")
        print(f"      提交→执行延迟 {gpu['ts'] - api['ts']} µs")
        for e in elders:
            print(f"[父层] {e['cat']:16} {e['name'][:50]}"
                  f"  [{e['ts']}, +{e['dur']}]")
```

**需要观察的现象**：每条 correlation 打印出 GPU、CPU API 两段记录、延迟数值，以及从外到内的父层链。

**预期结果**（train.json，作者已逐项核对）：

- `correlation 44633`：GPU `layer_norm::ln_fwd_kernel`（ts …471821、dur 112、stream 7）← `cudaLaunchKernel`（ts …471809、dur 11）← 父层依次 `ProfilerStep#1` → `1F1B` → `attn(F)` → `DropoutAddLayerNormFn`，延迟 12 µs。
- `correlation 44508`：GPU `dpsk::ep::internode::dispatch`（stream 27）← `cudaLaunchKernelExC`，延迟 998 µs；父层链落在哪一段调度注解里，留给读者运行查看（**待本地验证**——答案应该是 `dispatch(F)` 一类的注解，可与 u2-l2 的调度表对照）。

把脚本开头的文件名换成 `prefill.json`、corr 换成 146569，应复现 4.1.3 的第三组数字（延迟 15949 µs）；其最内层父算子为何，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：既然 `External id` 在本数据集里 join 不上，它还有用吗？
**答案**：有，但要分侧使用。CUDA 侧它与 `correlation` 相等，等于多一份校验字段；CPU 侧它是 `cpu_op`/`user_annotation` 自身的稳定编号，配合 `Ev Idx` 可以做「按记录顺序定位」「去重」等整理工作。只是**不要**拿它当跨层外键。

**练习 2**：区间嵌套法找父算子时，为什么必须限定**同一 `pid/tid`**？为什么用 API 记录的区间而不是 GPU 内核的区间去比？
**答案**：不同线程/进程上的事件区间没有包含语义（autograd 反向跑在 tid 2571，前向跑在 tid 1166，时间上本就交叠）；而 GPU 内核执行时刻（如 prefill 晚了 16 ms）早已离开提交它的算子的作用域，用内核的 ts 去套 CPU 区间只会落空。CPU API 调用是同步发生在算子作用域内的，所以用它的区间。

**练习 3**：如果一个 `cudaLaunchKernel` 的祖先链里同时出现 `cpu_op` 和 `user_annotation`，怎么判断谁更「外」？
**答案**：看区间端点：外层事件的区间完全覆盖内层。本讲样本中 `attn(F)`（dur 3282）覆盖 `DropoutAddLayerNormFn`（dur 124），后者覆盖 API 记录（dur 11）。脚本里对 `elders` 按 `ts` 升序排序即得「外 → 内」的链。

### 4.3 ac2g 流事件

#### 4.3.1 概念说明

`cat:"ac2g"`（async CPU to GPU）是 Chrome Trace **流事件**（flow event）：`ph:"s"`（start）画箭头起点、`ph:"f"`（finish）画箭头终点，靠相同的 `id` 配对。它不表示耗时，只表示「这两件事有关系」——具体说就是「这条 CPU 记录提交了那个 GPU 活动」。

在本仓库轨迹中，kineto（PyTorch Profiler 的采集后端）生成的 ac2g 有非常规整的形态：

- **真正的箭头**：每次启动类 API 调用处放一个 `s`（`id = correlation`，`ts` 等于 API 的 `ts`，位置在 CPU 线程轨道上），对应设备活动的起点处放一个 `f`（`id = correlation`，`ts` 等于内核的 `ts`，位置在 GPU 的 stream 轨道上，并带 `"bp": "e"` 表示绑定到切片边缘）。查看器据此从 CPU 的 API 色块画一根箭头射向 GPU 的内核色块——这就是 u1-l2 截图里那些斜向连线。
- **不成对的孤儿**：许多 CPU 侧 API（如 `cudaEventQuery`）后面跟了一个 `f`，其 `id` 在全文件找不到 `s`；另有少数 API（如 `cudaStreamWaitEvent`）带 `s` 却没有 GPU 侧的 `f`。孤儿流事件画不出箭头，统计时要排除。

#### 4.3.2 核心流程

train.json 的 ac2g 总账（数字均经作者核实，且 s 数 + f 数 = ac2g 总数，账目闭合）：

| 项 | 数量 | 说明 |
|---|---|---|
| `ph:"s"`（全在 CPU pid 1166） | 1048 | 983 个在启动类 API 处 + 56 个在 `cudaStreamWaitEvent` 处 + 9 个零星（未逐一归类） |
| `ph:"f"` 在 GPU（pid 0） | 983 | 恰好每个 kernel/memcpy/memset 一个，`ts` 等于设备活动开始时刻 |
| `ph:"f"` 在 CPU（pid 1166） | 2861 | 多挂在 `cudaEventQuery` 等非启动 API 后，按 `id` 找不到配对的 `s`（孤儿） |
| 合计 | 4876 | 与 u1-l4 统计的 ac2g 总数一致 |

于是「箭头数 = GPU 侧 f 数 = 设备活动数 = 启动类 API 数 = 983」，四个口径一个数，这根链条闭合得非常干净。prefill.json 同理：s 4845、f 25280（其中 GPU 侧 4152），ac2g 共 30125。

判断某条 s/f 是否构成箭头的流程：

```text
拿到一条 ac2g 事件 a
  ├─ 若 a.ph == "f" 且 a.pid 是 GPU 进程（元数据标签 "GPU 0"）
  │    → 一定是箭头终点；按 id=correlation 回到 CPU 的启动 API
  ├─ 若 a.ph == "s"
  │    → 在全文件找同 id 的 f：找到（通常在 GPU 轨道）则是一根箭头
  ├─ 其余（CPU 上的 f、无 f 的 s）
  └─ → 孤儿流事件，画不出箭头，统计配对时应忽略
```

#### 4.3.3 源码精读

回头看 4.1.3 黄金样本里夹着的三条事件，这次只看箭头：

[train.json:L31407-L31422](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31407-L31422) —— L31408：`{"ph": "f", "id": 44633, "pid": 0, "tid": 7, "ts": 1740461679471821, "cat": "ac2g", "name": "ac2g", "bp": "e"}`——箭头**终点**钉在 GPU 0 / stream 7 轨道上、内核开始执行的精确时刻；L31420：`{"ph": "s", "id": 44633, "pid": 1166, "tid": 1166, "ts": 1740461679471809, ...}`——箭头**起点**钉在 CPU 主线程上、`cudaLaunchKernel` 的开始时刻。同 `id` 44633 配对，查看器画出一根从 CPU 斜向下射到 GPU 的箭头。注意两条都没有 `dur`——流事件只占一个时刻。

[train.json:L31264-L31278](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31264-L31278) —— 通信流样本：`per_token_cast_to_fp8` 内核（stream 27）后面跟着 `f`（id 44479，L31264），CPU 侧 `cudaLaunchKernel`（correlation 44479，L31268）后面跟着 `s`（L31276）。四条记录两两成对，结构与 stream 7 的样本完全一致——**箭头机制不区分计算流与通信流**。

再看孤儿的模样：

[train.json:L31200-L31210](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31200-L31210) —— `cudaEventQuery`（correlation 44463）后面跟着 `ph:"f"`、id 44463 的 ac2g 事件，位置就在 CPU 线程自己身上。用 `id: 44463` 全文检索只会命中这一条——没有任何 `s` 与它配对，是一根画不出来的「断头箭头」。这类事件占 CPU 侧 f 的多数，是统计箭头时最大的噪声源。

prefill 中的箭头同样规整：

[prefill.json:L205261-L205272](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L205261-L205272) —— `_layer_norm_kernel` 后跟 `f`（id 146569、pid 0、tid 7），`cuLaunchKernel` 后跟 `s`（id 146569、pid 1097、tid 1097）。这根箭头横跨约 16 ms 的排队延迟，在 chrome://tracing 里放大后清晰可见——箭头越长，说明提交与执行离得越远。

[prefill.json:L570403-L570413](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L570403-L570413) —— `process_name`/`process_labels` 元数据把 pid 1097 标记为 `CPU`（进程名 `python3`）——读 prefill 的箭头前先确认这一份的 CPU 进程号是 1097 而非 train 的 1166。

#### 4.3.4 代码实践

**实践目标**：统计 ac2g 流事件的配对情况，验证「GPU 侧 f 数 = 设备活动数 = 真箭头数」。

**操作步骤**（示例代码）：

```python
# audit_flows.py —— 示例代码
import json
from collections import Counter

evts = json.load(open("train.json", encoding="utf-8"))["traceEvents"]

gpu_pids = {e["pid"] for e in evts if e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset")}
dev_cnt  = Counter(e["cat"] for e in evts if e.get("cat") in
                   ("kernel", "gpu_memcpy", "gpu_memset"))

starts = [e for e in evts if e.get("cat") == "ac2g" and e.get("ph") == "s"]
ends   = [e for e in evts if e.get("cat") == "ac2g" and e.get("ph") == "f"]
s_ids, f_ids = {e["id"] for e in starts}, {e["id"] for e in ends}
f_on_gpu = [e for e in ends if e["pid"] in gpu_pids]

print("设备侧活动:", dict(dev_cnt), "合计", sum(dev_cnt.values()))
print(f"s 总数 {len(starts)}  f 总数 {len(ends)}  其中 GPU 侧 f {len(f_on_gpu)}")
print(f"配成箭头的 id 数 {len(s_ids & f_ids)}；孤儿 s {len(s_ids - f_ids)}；孤儿 f {len(f_ids - s_ids)}")
# 抽验一根箭头：起点在 CPU 的哪条 API、终点在 GPU 的哪个内核
arrow_id = sorted(s_ids & f_ids)[0]
for e in starts + ends + evts:
    if e.get("cat") == "ac2g" and e.get("id") == arrow_id:
        print(arrow_id, e["ph"], "pid", e["pid"], "tid", e["tid"], "ts", e["ts"])
        break
```

**需要观察的现象**：设备侧活动数与 GPU 侧 f 数是否相等；配对 id 数、两类孤儿各多少。

**预期结果**（train.json，作者已核实）：设备侧 `kernel 963 + gpu_memcpy 8 + gpu_memset 12 = 983`；s 1048、f 3844、其中 GPU 侧 f 983；配成箭头的 id 983 个；孤儿 s 65（= 1048 − 983）；孤儿 f 2861。换成 prefill.json 应得：设备侧 4152、GPU 侧 f 4152、配对 4152（s/f 总数分别为 4845 / 25280，**prefill 的孤儿数留给你运行计算，待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：chrome://tracing 里看到一根从 CPU 轨道射向 GPU 轨道的箭头，它对应数据里的哪几条记录？
**答案**：两条 `cat:"ac2g"` 事件——一个 `ph:"s"`（起点，钉在 CPU 上启动类 API 的 `ts` 处）与一个 `ph:"f"`（终点，钉在 GPU 上设备活动的 `ts` 处），二者 `id` 相同且等于那次提交的 `correlation`；箭头两端的色块则分别是那条 `cuda_runtime` 记录和那个 `kernel` 记录。

**练习 2**：为什么 train.json 的 ac2g 有 4876 条，箭头却只有 983 根？
**答案**：4876 = s 1048 + f 3844。只有 `id` 能配上对的 s/f 才画箭头（983 对）；其余是孤儿——2861 条 CPU 侧的 `f`（多在 `cudaEventQuery` 等非启动 API 后，如 id 44463 全文仅出现一次）和 65 条没有终点的 `s`（多在 `cudaStreamWaitEvent` 处）。做「箭头级」统计前必须先按 id 过滤。

**练习 3**：`f` 事件里的 `"bp": "e"` 是什么意思？没有 `dur` 字段又说明什么？
**答案**：`bp`（binding point）指定箭头终点绑定到目标切片的边缘（`"e"` = enclosing/edge），保证箭头钉在内核色块上；流事件是瞬时事件，只标记时刻不占时长，所以没有 `dur`——这也是 u1-l4 强调「算时间只统计 `ph:"X"`」的原因之一。

## 5. 综合实践

把 4.1–4.3 的三个脚本合并成一个「轨迹链路检查器」`trace_correlate.py`，对任意一份轨迹完成三级跳：

1. **输入**：轨迹路径 + 一个内核名片段（或 `correlation`）。
2. **第一跳**：找到目标 `kernel` 事件，取 `args.correlation` 与 `args.stream`。
3. **第二跳**：定位同 `correlation` 的 `cuda_runtime`/`cuda_driver` 事件，打印 API 名、`cbid`、提交时刻与延迟 \( ts_{kernel} - ts_{api} \)。
4. **第三跳**：用同 `pid/tid` 区间嵌套打印由外到内的祖先链（`user_annotation` → `cpu_op`）。
5. **附加**：输出该轨迹的账本——启动类 API 数、设备侧活动数、GPU 侧 f 数、配对箭头数，四个数字应当一致。

执行方案：

- 对 train.json 分别以 `ln_fwd_kernel` 与 `internode::dispatch` 为目标各跑一次，记录两条链的完整输出与延迟（应为 12 µs 与 998 µs）。
- 对 prefill.json 以 `_layer_norm_kernel` 为目标跑一次，观察 15949 µs 的深排队延迟，并记下它的祖先注解名（**待本地验证**——u2-l4 将按注解与流的结构系统梳理 prefill）。
- 把三份输出并排成一张表：目标内核 | 所在流 | 启动 API | 延迟 | 祖注解链。这张表就是你此后阅读所有轨迹的「个人字典」。

若某个数字对不上（例如设备活动数 ≠ GPU 侧 f 数），优先检查：是否把 `cat:"cuda_driver"` 漏在启动 API 之外（train 的 40 个 `cuLaunchKernel` 全在此类）、是否误把孤儿 f 计入箭头、是否跨进程混用了 pid（train 是 1166，prefill 是 1097）。

## 6. 本讲小结

- 一次内核启动在轨迹中是三层记录：`cpu_op`（算子，CPU）→ `cuda_runtime`/`cuda_driver`（API 调用，CPU）→ `kernel`/`gpu_memcpy`/`gpu_memset`（实际执行，GPU）；CPU 与 GPU 靠异步队列衔接，提交与执行是两个时刻。
- `args.correlation` 是 CUDA API 记录与设备活动记录之间**一对一**的外键：train.json 两侧严格各 983 条（727 `cudaLaunchKernel` + 196 `cudaLaunchKernelExC` + 40 `cuLaunchKernel` + 8 `cudaMemcpyAsync` + 12 `cudaMemsetAsync`），prefill.json 各 4152 条。
- 三个编号要分清：`Ev Idx` 是 CPU 侧记录顺序号；CPU 侧 `External id` 是算子编号；CUDA 侧 `External id` 在本仓库实测与 `correlation` 相等且不与任何 `cpu_op` 重合——**跨层追算子请用同线程区间嵌套法**，不要拿 `External id` join。
- 区间嵌套法实证链：`ProfilerStep#1` → `1F1B` → `attn(F)` → `DropoutAddLayerNormFn` → `cudaLaunchKernel`(correlation 44633) → GPU stream 7 的 `ln_fwd_kernel`，语义与名字互相印证。
- `ac2g` 流事件（`ph:"s"`/`f"`，同 `id` 配对）是查看器里 CPU→GPU 箭头的数据来源；真箭头 983 根（train）恰好等于设备活动数，其余 2861 + 65 条是不成对的孤儿，统计时要过滤。
- 提交→执行延迟 \( ts_{kernel} - ts_{api} \) 是观察排队深度的窗口：train 的 `ln_fwd` 仅 12 µs（GPU 即取即执行），通信流 `dispatch` 998 µs（排在同流 `cached_notify` 之后），prefill 的 `_layer_norm_kernel` 高达 15949 µs（吞吐模式下 CPU 领先近 16 ms 的深排队）。

## 7. 下一步学习建议

工具已经就位：你现在能把任何一个 GPU 内核翻译成「哪个调度阶段里的哪个算子、通过哪次 API 调用提交」。下一讲 **u2-l2《train.json 与 DualPipe：1F1B 调度注解》**正好用这套工具去读 train.json 的 42 条 `user_annotation`——`ProfilerStep#1`、`1F1B` 总注解，以及 `attn/mlp/dispatch/combine × F/B/W` 的分块调度序列，验证一个 chunk 内 4 个 MoE 层的「先反后前」交错模式。建议：先把本讲的 `trace_link.py` 跑通并留存三份输出（u2-l4、u3-l1 都会直接复用它的骨架）；再重看 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg)——现在图中 CPU 轨道与 GPU 轨道之间的每根箭头，你都应该能说出它对应的 `correlation` 与三层记录了。
