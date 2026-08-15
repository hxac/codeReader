# 算子大类巡礼：norm、pooling、loss、index、vfusion 等

## 1. 本讲目标

前面几个单元里，我们反复用 `add_example`（elementwise 教学样例）、`gelu`（生产级 elementwise 算子）、`quant_batch_matmul_v4`（Cube 量化融合算子）作为样本。本讲把视野拉高到整个仓库：ops-nn 有十几个算子大类、数百个算子目录，它们共享同一套「op_host / op_kernel / op_api / op_graph」交付件骨架，但在 tiling 策略、kernel 编程模型、芯片适配上呈现出明显的大类差异。

学完本讲，你应该能够：

1. 说出仓库里有哪些算子大类、各自大约的规模，以及去哪里查一个算子的调用能力（`docs/zh/op_list.md`）。
2. 理解大类目录的 CMake 构建组织方式——为什么新增算子目录不需要改大类级 CMakeLists。
3. 通过 norm（rms_norm）、loss（cross_entropy_loss）、index（gather_v2）、vfusion（multi_scale_deformable_attn_function）四个真实样本，识别归一化/损失/索引/融合算子在 tiling 与 kernel 上的典型差异。
4. 建立「按需求定位候选参考算子」的检索能力：接到一个新算子需求时，知道先去哪个大类找参考实现。

## 2. 前置知识

本讲假设你已经掌握以下前置内容（来自前面讲义），这里只做简要回顾：

- **算子工程五层交付件**（u1-l3）：每个算子目录下 `op_host`（def/infershape/tiling）、`op_kernel`（AI Core 实现）、`op_api`（aclnn 适配）、`op_graph`（GE 图模式 proto）各司其职，缺哪个目录就缺哪种能力。
- **Tiling 两级切分与 TilingKey**（u4-l1、u4-l2）：Host 侧 tiling 回调产出 TilingData（POD 字节契约）、BlockDim、TilingKey；TilingKey 是 uint64 的「二进制选择器」，kernel 入口按它分发到不同模板实例。
- **矢量算子三段式流水**（u5-l1）：CopyIn → Compute → CopyOut，TPipe/TQue 管理 UB 缓冲，双缓冲让搬运与计算重叠。
- **多架构目录 arch35**（u5-l3）：生产算子会把特定芯片（如 ascend950 系列）的实现下沉到 `arch35/` 子目录，由 CMake 开关控制只编给对应芯片。

本讲新引入的术语：

- **归约（reduce）**：沿某一维求和/求最大值等操作。elementwise 算子每个输出只依赖一个输入元素，而归约算子的一个输出依赖一整行（或一整片）输入，这直接改变了 tiling 的切分维度。
- **SIMT 编程模型**：`__simt_vf__` 标注的线程级写法，类似 CUDA 的「一个线程处理若干元素」，与传统的「整块搬运 + 矢量指令」模型相对，适合访存不规则（如 gather 按 indices 跳转）的场景。
- **AI CPU 实现**：算子跑在 AI CPU（控制核）而非 AI Core 上，用 `op_kernel_aicpu` 目录交付，常见于数据搬运/索引类算子在部分芯片上的兜底实现。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| `docs/zh/op_list.md` | 全仓库算子总清单：每个算子的 op_kernel/op_host/op_api/op_graph 支持情况、执行硬件单元、一句话说明 |
| `norm/CMakeLists.txt`、`loss/CMakeLists.txt`、`index/CMakeLists.txt`、`vfusion/CMakeLists.txt` | 大类目录的构建入口，展示 file(GLOB) 自动收集子目录的机制 |
| `norm/rms_norm/` | norm 类代表算子：多策略 tiling（NORMAL/SPLIT_D/MERGE_N/SINGLE_ROW 四种模式） |
| `loss/cross_entropy_loss/` | loss 类代表算子：base/特化分层的 kernel 组织，归约求和 |
| `index/gather_v2/` | index 类代表算子：AI CPU 与 AI Core（arch35 SIMT）双实现并存 |
| `vfusion/multi_scale_deformable_attn_function/` | vfusion 类代表算子：按 `__CCE_AICORE__` 宏区分 310P 与通用/高性能两代实现 |

## 4. 核心概念与源码讲解

### 4.1 仓库算子版图：十四个大类与 op_list 总清单

#### 4.1.1 概念说明

ops-nn 顶层目录里每个「算子大类」是一个目录，内部挂数十个算子工程目录。按目录条目粗略统计（排除 CMakeLists、common 等非算子目录，数字为约数）：

| 大类 | 算子数（约） | 典型算子 | 计算特征 |
| --- | --- | --- | --- |
| index | 90 | gather_v2、embedding、index_put | 不规则访存，按索引跳转 |
| activation | 82 | gelu、silu、celu | elementwise，逐元素映射 |
| norm | 71 | rms_norm、layer_norm、group_norm | 沿行/通道归约 + 缩放 |
| foreach | 67 | foreach_add_list、foreach_abs | 张量列表批量 elementwise |
| optim | 52 | adam_apply_one、apply_adagrad | 优化器状态更新，多输入 inplace |
| quant | 46 | ascend_quant、acts_ulq | 量化/反量化，多为 elementwise |
| loss | 38 | cross_entropy_loss、binary_cross_entropy | 归约 + 超越函数 |
| matmul | 32 | quant_batch_matmul_v4 | Cube 矩阵乘，FRACTAL 分块 |
| pooling | 29 | avg_pool、adaptive_max_pool2d | 滑窗/自适应窗口归约 |
| conv | 15 | conv2d、conv3d | 卷积（im2col 或直接滑窗） |
| rnn | 9 | dynamic_rnn、thnn_fused_lstm_cell | 循环依赖，时序串行 |
| vfusion | 7 | multi_scale_deformable_attn_function | 特定模型融合算子 |
| hash | 5 | embedding_hash_table_* | 哈希表查插 |
| control | 2 | assert、sleep | 流程控制，非计算 |

#### 4.1.2 核心流程

查一个算子的标准动线：

1. 打开 `docs/zh/op_list.md`，按「算子分类」列定位大类。
2. 找到目标算子行，看四个能力列（op_kernel / op_host / op_api / op_graph）的 ✓/✗：
   - ✓ op_api → 支持 aclnn eager 调用；
   - ✓ op_graph → 支持图模式；
   - 「算子执行硬件单元」列标 AI Core / AI CPU / AI Core/AI CPU。
3. 点算子目录链接进 README，看支持的 dtype/format 与约束。

#### 4.1.3 源码精读

总清单的表头定义了能力矩阵的语义：

[docs/zh/op_list.md:L15-L24](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L15-L24)

这几行是 HTML 表格的表头：算子分类、算子目录、op_kernel/op_host（算子实现）、op_api（aclnn 调用）、op_graph（图模式调用）、算子执行硬件单元、说明。每个算子一行。

看 gather_v2 这一行：

[docs/zh/op_list.md:L1760-L1770](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L1760-L1770)

这行说明 gather_v2 四项能力全 ✓，但执行硬件是「AI Core/AI CPU」，且备注写着「该算子暂无Ascend C代码实现，欢迎开发者补充贡献」——这句话与目录里同时存在 `op_kernel_aicpu/` 和 `op_kernel/arch35/` 形成有趣对照：老芯片上走 AI CPU 实现，新芯片（arch35，即 ascend950 系列）已有 Ascend C 实现落地。这是阅读总清单时要留意的「能力随芯片代际演进」现象。

再看 loss 类的一行：

[docs/zh/op_list.md:L2360-L2369](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/op_list.md#L2360-L2369)

cross_entropy_loss：op_kernel/op_host/op_api 均 ✓，op_graph 为 ✗（不支持图模式），执行硬件 AI Core。

#### 4.1.4 代码实践

1. **实践目标**：学会用 op_list 快速判断算子能力。
2. **操作步骤**：在 `docs/zh/op_list.md` 中检索 `norm/rms_norm`、`index/embedding`、`vfusion/scaled_masked_softmax_v2` 三行，记录各自的四项能力与硬件单元。
3. **需要观察的现象**：哪些算子 op_api ✗ 但 op_graph ✓（只入图、不可 eager 调用）；哪些标了 AI CPU。
4. **预期结果**：能得到一张三行的能力对照表。（本操作纯文件检索，可直接完成。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 op_list 要把 op_kernel/op_host 与 op_api/op_graph 分开列？

**答案**：op_kernel/op_host 表示「算子本身有没有实现」，op_api/op_graph 表示「以哪种方式暴露给用户」。二者正交：有的算子只有实现没有 aclnn 适配（只能图模式用），有的算子（如部分 v2 版本）复用底层实现、只补 op_api 壳。

**练习 2**：一个算子行标注「AI Core/AI CPU」意味着什么？

**答案**：该算子在不同芯片型号上走不同执行单元——某些芯片上有 AI Core 实现，另一些（或某些 shape 场景）落到 AI CPU 实现，运行时由框架按芯片与场景选择，目录上通常对应 `op_kernel` 与 `op_kernel_aicpu` 并存。

### 4.2 大类目录的构建组织：file(GLOB) 自动收集

#### 4.2.1 概念说明

十四个大类目录下各自只有一个 CMakeLists.txt，却管理着几十个算子的编译。关键机制是 **glob 自动发现**：CMake 用 `file(GLOB)` 枚举大类下所有子目录，凡是带 `CMakeLists.txt`（或 `op_host/CMakeLists.txt`）的子目录自动 `add_subdirectory`。这解释了 u1-l2 里的一个现象：`build.sh --ops=xxx` 单算子编译能自动解析依赖算子——因为整棵算子树都是按目录约定自动挂载的，新增算子目录不需要在大类 CMakeLists 里登记。

#### 4.2.2 核心流程

```
顶层 CMakeLists.txt
  └── add_subdirectory(norm / loss / index / vfusion / ...)
        └── file(GLOB) 枚举子目录
              └── 子目录有 CMakeLists.txt？
                    ├── 是 → add_subdirectory(子目录)          # 完整算子工程
                    └── 否 → 有 op_host/CMakeLists.txt？
                              ├── 是 → add_subdirectory(op_host) # 仅 host 交付件
                              └── 否 → 跳过                     # docs/common 等非工程目录
```

#### 4.2.3 源码精读

norm 大类的收集逻辑：

[norm/CMakeLists.txt:L12-L21](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/CMakeLists.txt#L12-L21)

`file(GLOB CURRENT_DIRS ...)` 把 norm 下所有条目列出来，foreach 循环里先看子目录自身有没有 CMakeLists.txt，没有再看 `op_host/CMakeLists.txt`——两级探测保证「完整工程」和「只有 host 交付件的算子」都能被编入，而 common、docs 这类纯辅助目录自然被跳过。

loss 大类是简化写法，只做第一级探测：

[loss/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/loss/CMakeLists.txt#L11-L19)

`LIST_DIRECTORIES true` 让 GLOB 返回目录名，随后只检查 `${SUBDIR}/CMakeLists.txt` 是否存在。

index 与 vfusion 则是两级探测的完整写法：

[index/CMakeLists.txt:L12-L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/index/CMakeLists.txt#L12-L22)
[vfusion/CMakeLists.txt:L11-L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/vfusion/CMakeLists.txt#L11-L22)

两者还都设置了 `OPTEST_NAME optest_${PKG_NAME}`，为该大类挂接测试目标。

#### 4.2.4 代码实践

1. **实践目标**：验证「新增算子目录零登记」。
2. **操作步骤**：对比 `pooling/CMakeLists.txt` 与 `vfusion/CMakeLists.txt` 的写法差异；然后在本地新建一个空目录 `norm/my_fake_op/`（试验后删除），放入一个仅包含 `message(STATUS "my_fake_op hooked")` 的 `CMakeLists.txt`，跑一次 cmake 配置。
3. **需要观察的现象**：cmake 输出中出现 `my_fake_op hooked`，且没有修改过 norm/CMakeLists.txt。
4. **预期结果**：确认 glob 收集机制生效。（若本地无完整 CANN 环境无法配置，可在源码层面确认逻辑，标注「待本地验证」。）

#### 4.2.5 小练习与答案

**练习**：为什么有的写法要探测 `op_host/CMakeLists.txt`，有的不用？

**答案**：探测 op_host 是为了收录「只有 Host 侧交付件、无自有 kernel」的算子工程（例如纯 aclnn 适配或复用其他算子 kernel 的 v2/v3 版本）。loss 大类下的算子普遍带完整 op_kernel，每个工程都有自己的顶层 CMakeLists.txt，所以只需第一级探测。

### 4.3 norm 类：rms_norm 的多策略 tiling

#### 4.3.1 概念说明

norm 类算子（RMSNorm、LayerNorm、GroupNorm…）是 LLM 推理的核心开销之一。与 elementwise 的本质区别：**输出 y 的每一行依赖输入 x 的同一整行**——必须先算整行平方和（归约），再逐元素缩放。这带来两个工程后果：

1. tiling 不能只按「元素总数」一维切，还要按「行 × 列」两维规划：行方向多核并行，列方向受 UB 容量约束可能要分段累加。
2. 一行数据量（numCol）从 128 到 5 万变化时，最优策略完全不同，因此 rms_norm 的 tiling 里出现了**模式选择**：不同 shape/芯片组合选不同 mode，每个 mode 对应一份 kernel 实现。

#### 4.3.2 核心流程

rms_norm 的 Host 侧 tiling 决策（简化伪代码）：

```
读 shape：numRow（行数）、numCol（每行元素数）、dtype、SocVersion
默认 modeKey = MODE_NORMAL            # 单核一次装下一行
若 numColAlign > ubFactor：
    modeKey = MODE_SPLIT_D            # 行太长，一行拆多段，归约分段累加
若 (numCol 小 && 芯片==ASCEND910B) 或 (310P 特定 shape)：
    modeKey = MODE_MERGE_N            # 多行合并处理，榨吞吐
（另一分支：numRow==1 时）modeKey = MODE_SINGLE_ROW   # 单行大服务，多核合力算一行
tilingKey = 加权编码(modeKey, colAlign, SocVersion, dtypeKey)
SetBlockDim(useCoreNum); SetTilingKey(tilingKey)
```

Device 侧按 tilingKey 分发到 `rms_norm_single_row.h` / `rms_norm_split_d.h` / `rms_norm_merge_n.h` / `rms_norm_whole_reduce_sum.h` 等不同实现文件。

#### 4.3.3 源码精读

模式常量定义（NORMAL/SPLIT_D 在 tiling 头文件，MERGE_N/SINGLE_ROW 在 tiling 源文件）：

[norm/rms_norm/op_host/rms_norm_tiling.h:L82-L83](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_host/rms_norm_tiling.h#L82-L83)
[norm/rms_norm/op_host/rms_norm_tiling.cpp:L50-L51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_host/rms_norm_tiling.cpp#L50-L51)

四个模式值 0/1/2/3，就是 kernel 分发的第一层选择。

tiling 主流程中的模式选择与 MERGE_N 特调：

[norm/rms_norm/op_host/rms_norm_tiling.cpp:L444-L470](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_host/rms_norm_tiling.cpp#L444-L470)

注意这段代码里 tiling 逻辑直接耦合了芯片型号判断（`SocVersion::ASCEND910B`、`ASCEND310P`），甚至为 310P 预留了 BRCB 广播指令的 UB 空间（`BRCB_RESERVED_UB_USED`）——这是「tiling 是性能工程而非纯数学」的直接证据。

tilingKey 的加权编码：

[norm/rms_norm/op_host/rms_norm_tiling.cpp:L496-L505](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_host/rms_norm_tiling.cpp#L496-L505)

NORMAL 模式下 `tilingKey = colAlign * 100 + SocVersion * 1000 (+ dtypeKey * 10)`，即用十进制位段把「对齐宽度、芯片、dtype」打包进一个 uint64；Regbase 架构则简化为 `SocVersion * 1000 + modeKey`。相比 add_example 只用 0/1 区分 float/int32，这里 TilingKey 承载的信息量大得多。

kernel 侧按行划分 GM 窗口：

[norm/rms_norm/op_kernel/rms_norm.h:L22-L58](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_kernel/rms_norm.h#L22-L58)

`KernelRmsNorm` 继承 `KernelRmsNormBase`，Init 里每个核按 `blockIdx_ * block_factor * num_col` 计算自己负责的行区间（核并行在**行**方向，而 add_example 是在扁平元素方向）；还为 FP16/BF16 额外分配 float 中转缓冲（`x_fp32_buf`、`sqx_buf`），延续 gelu 的「低精度输入、FP32 累加」精度策略；`#if __CCE_AICORE__ == 200` 分支是 310P 特定的 rstd 预处理。op_kernel 目录下 `rms_norm_single_row.h`、`rms_norm_split_d.h`、`rms_norm_merge_n.h`、`rms_norm_whole_reduce_sum.h` 各对应一种 tiling 模式——**一个算子、多份 kernel 策略，靠 tilingKey 连接**。

#### 4.3.4 代码实践

1. **实践目标**：理解 shape 如何驱动模式选择。
2. **操作步骤**：阅读 [norm/rms_norm/op_host/rms_norm_tiling.cpp:L440-L505](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/norm/rms_norm/op_host/rms_norm_tiling.cpp#L440-L505)，人工推演三组输入在 ascend910b 上的 modeKey：(a) numRow=1024, numCol=256；(b) numRow=8, numCol=8192；(c) numRow=1, numCol=65536。再对照 rms_norm 的 UT/st 目录（如有）确认推演。
3. **需要观察的现象**：三组分别命中 NORMAL（或 MERGE_N）、SPLIT_D 候选、SINGLE_ROW 的判定路径。
4. **预期结果**：写出每组 shape → modeKey → kernel 实现文件 的三行结论。模式判定散布在 714 行的 tiling 文件多个分支中，具体取值待本地用 UT 打印验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 rms_norm 需要四种模式而 add_example 只有一种？

**答案**：add_example 的每个输出只依赖同位置输入，任意切分都不影响语义；rms_norm 的输出依赖整行归约，行太长（UB 装不下）、行太短（并行度不足）、只有一行（无行维并行）分别是三种不同的困境，需要不同的数据组织与核间协作方式，因此按场景分模式实现。

**练习 2**：`ub_factor * sizeof(T)` 之外为什么还要 `x_fp32_buf`、`sqx_buf` 两个 float 缓冲？

**答案**：FP16/BF16 输入的平方和累加若在原精度下进行会快速损失精度（大模型隐藏维常为数千），所以先在 float 精度下算 x² 与累加，归一化后再转回原 dtype 输出。这是 norm 类算子的通用精度套路。

### 4.4 loss 类：cross_entropy_loss 的 base/特化分层

#### 4.4.1 概念说明

loss 类算子（交叉熵、CTC、余弦嵌入损失等）的公共骨架是「log_softmax/掩码/加权 → 沿 batch 维归约求均值」。它和 norm 一样是归约型算子，但工程组织上有自己的特色：kernel 用**基类 + 特化层**的两层继承——`cross_entropy_loss_base.h` 放公共流程（搬运、掩码、加权），`cross_entropy_loss.h` 放主实现，`cross_entropy_loss_fp32.h` 放 FP32 专用路径，`cross_entropy_loss.cpp` 是入口。这和 rms_norm 的「按模式拆多个平级文件」是两种不同的拆分哲学。

#### 4.4.2 核心流程

```
kernel 入口 (cross_entropy_loss.cpp)
  └── 按 tilingKey/dtype 分发
        ├── CrossEntropyLoss<half>     ← cross_entropy_loss.h（通用，内部升 float 计算）
        └── CrossEntropyLossFp32       ← cross_entropy_loss_fp32.h（原生 FP32 路径）
主流程：CopyIn(input/target/weight)
  → Compute：log_softmax + NLL 索引取值 + zloss 修正（可选）
  → GetSumLoss：沿 batch 维归约求和再除以 N
  → CopyOut(loss, logProb)
```

#### 4.4.3 源码精读

主 kernel 类继承基类：

[loss/cross_entropy_loss/op_kernel/cross_entropy_loss.h:L23-L30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/loss/cross_entropy_loss/op_kernel/cross_entropy_loss.h#L23-L30)

`CrossEntropyLoss<OriT>` 继承 `CrossEntropyLossBase<OriT>`，Init/Process 的签名比 add_example 长得多：input、target、weight、loss、logProb、zloss、lseForZloss、workspace、tilingData——loss 算子天然输入输出多，且 zloss/lseForZloss 是可选输出（对应 def 里的 OPTIONAL 参数）。

归约求和的落点：

[loss/cross_entropy_loss/op_kernel/cross_entropy_loss.h:L120-L130](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/loss/cross_entropy_loss/op_kernel/cross_entropy_loss.h#L120-L130)

`GetSumLoss` 里 `GetReduceSum(lnLocal, workLocal, reduceRes, len)` 完成 batch 维归约；后面那个三元表达式 `len > NUM_4096 && len < FP32_128_REPEAT ? reduceRes(0)+reduceRes(1) : reduceRes(0)` 是对特定长度区间归约结果布局的修正——这类「魔法数修正」在归约算子里很常见，阅读时结合 UT 断言理解。

op_host 侧结构与 rms_norm 同构（def + infershape + tiling + tiling_arch35）：

[loss/cross_entropy_loss/op_host/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/loss/cross_entropy_loss/op_host/)（目录清单：`cross_entropy_loss_def.cpp`、`cross_entropy_loss_infershape.cpp`、`cross_entropy_loss_tiling.cpp`、`cross_entropy_loss_tiling.h`、`cross_entropy_loss_tiling_arch35.cpp`、`config/`）

可见 u1-l3 建立的目录契约在 loss 大类同样成立，`_tiling_arch35.cpp` 是 ascend950 系列专用的 tiling 变体。

#### 4.4.4 代码实践

1. **实践目标**：体会「多输入 + 可选输出」的 infershape 写法。
2. **操作步骤**：打开 `loss/cross_entropy_loss/op_host/cross_entropy_loss_infershape.cpp`，找出 target/weight 的 shape 校验（target 通常为 `[N]` 或 `[N, d1, ...]` 依赖 reduction 参数），以及 zloss 等可选输出如何依赖 attr 决定是否有值。
3. **需要观察的现象**：reduction='none'/'mean'/'sum' 三种取值如何改变输出 shape 推导。
4. **预期结果**：能复述「attr 参与 shape 推导」这一与 add_example（纯复制 shape）的关键差异。具体分支待阅读源码后确认。

#### 4.4.5 小练习与答案

**练习**：cross_entropy_loss 与 rms_norm 同为归约型，为何文件组织一个选「基类继承」、一个选「多平级模式文件」？

**答案**：rms_norm 的四种模式数据流差异大（核间协作方式都不同），平级文件各自成章更清晰；cross_entropy_loss 的 FP32 与半精度路径流程相同、仅计算精度不同，公共流程抽到 base 里复用更省。选择哪种组织方式取决于「差异发生在流程级还是参数级」。

### 4.5 index 类：gather_v2 的双实现与 SIMT 编程模型

#### 4.5.1 概念说明

index 类是仓库最大的大类（约 90 个算子）：gather/scatter/embedding/index_put 等。它们的共同特征是**不规则访存**——要读哪个元素由 indices 张量运行期决定，Host 侧 tiling 无法预知访存模式。这催生了两条实现路线：

1. **AI CPU 实现**（`op_kernel_aicpu/`）：标量 C++ 代码，逐元素按索引搬运，开发简单、适配任意不规则度，作为多数芯片上的实现。
2. **AI Core + SIMT 实现**（`op_kernel/arch35/`）：在 ascend950 系列上用 `__simt_vf__` 线程级编程模型（类似 CUDA：一个线程用 LAUNCH_BOUND 约束、处理一段输出元素），让每个线程独立跟踪自己的索引，避开「整块矢量搬运」对连续性的要求。

#### 4.5.2 核心流程

gather_v2 的交付件全景：

```
index/gather_v2/
├── op_graph/gather_v2_proto.h + fusion_pass/   # GE 图模式原型与融合规则
├── op_host/
│   ├── gather_v2_def.cpp / gather_v2_infershape.cpp   # 通用 def 与 shape 推导
│   └── arch35/gather_v2_tiling*.cpp/h          # 950 系列：tiling + TilingParse 两段注册
├── op_kernel/
│   └── arch35/gather_v2*.h                     # 950 系列：6 种 kernel 变体
└── op_kernel_aicpu/gather_v2_aicpu.*           # AI CPU 实现（老芯片主力）
```

arch35 tiling 按 shape 特征在多个 tilingKey（SIMD / SIMD_TWO_DIM / SIMD_LAST_GATHER / SIMD_GA_ALL_LOAD / SIMT_TWO_DIM / EMPTY）中选一个，kernel 入口按 key 进入对应变体。

#### 4.5.3 源码精读

tiling 的两段注册（u5-l3 在 gelu 见过的 TilingParse 模式）：

[index/gather_v2/op_host/arch35/gather_v2_tiling_arch35.cpp:L1474-L1475](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/index/gather_v2/op_host/arch35/gather_v2_tiling_arch35.cpp#L1474-L1475)

一行代码同时注册了 GatherV2 和 Gather 两个算子名（历史别名共享实现）：`IMPL_OP_OPTILING(GatherV2).Tiling(GatherTiling).TilingParse<GatherV2CompileInfo>(TilingPrepareForGatherV2)`——Tiling 在运行期回调，TilingParse 在编译期把 AIV 核数等平台信息固化进 CompileInfo。

tilingKey 的多路选择：

[index/gather_v2/op_host/arch35/gather_v2_tiling.cpp:L61-L71](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/index/gather_v2/op_host/arch35/gather_v2_tiling.cpp#L61-L71)

这些 key 值（如 `SIMD_TILING_KEY = 1000000099UL`）是十亿级大数——与 rms_norm 的位段编码不同，index 算子直接用大数常量做 key，语义对应六种 kernel 变体。选 key 逻辑见同文件 L652-L670 的一串 if。

kernel 侧的 SIMT 写法：

[index/gather_v2/op_kernel/arch35/gather_v2.h:L27-L61](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/index/gather_v2/op_kernel/arch35/gather_v2.h#L27-L61)

三处与 add_example 截然不同的细节：

- `__simt_vf__` 修饰 `GatherSimt` 静态成员函数——SIMT 矢量取数编程模型入口，函数按「线程」执行；
- `LAUNCH_BOUND(THREAD_NUM_LAUNCH_BOUND)`（512/2048，按 `__DAV_FPGA__` 区分仿真与真机）——声明每核线程上限，帮助编译器分配寄存器；
- 参数表里 `m0, shift0, m1, shift1, m2, shift2`——把下标乘除法预先拆成「乘 + 移位」的编译期友好形式，这是索引算子常见的强度削减优化。

AI CPU 实现的形态：

[index/gather_v2/op_kernel_aicpu/gather_v2_aicpu.cpp:L16-L30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/index/gather_v2/op_kernel_aicpu/gather_v2_aicpu.cpp#L16-L30)

包含 `cpu_types.h`、`utils/kernel_util.h`，用 `std::vector`/普通 C++ 写成——没有 TPipe/TQue/LocalTensor，是一段跑在控制核上的标量程序。同目录还有独立的 `gather_v2_aicpu_def.cpp`（AI CPU 侧的算子注册）。

#### 4.5.4 代码实践

1. **实践目标**：对比同一算子的 AI CPU 与 AI Core 两份实现的复杂度。
2. **操作步骤**：先读 `index/gather_v2/op_kernel_aicpu/gather_v2_aicpu.cpp` 的主计算函数（约百行标量循环）；再读 `index/gather_v2/op_kernel/arch35/gather_v2_simd.h` 与 `gather_v2_simt_two_dim.h` 的 Process；最后列出两份实现各自处理的边界（indices 为空、axis 负数、batch_dims 属性等）。
3. **需要观察的现象**：AI CPU 版是纯 C++ 逻辑直译；AI Core 版把同一语义拆成按数据布局（一维/二维/末维 gather）的多份特化。
4. **预期结果**：写出一页对比笔记，回答「什么情况下索引算子值得上 AI Core」——indices 规模大、gather 维度规则时收益明显；极端不规则时 AI CPU 更稳。（运行对比需 950 环境与老芯片环境各一，待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 gather 类算子适合 SIMT 模型而 elementwise 适合「整块搬运 + 矢量指令」？

**答案**：elementwise 的 GM 访存地址连续，DataCopy 整块搬运效率最高；gather 的目标地址由 indices 决定、彼此无关，整块搬运无从谈起，而 SIMT 让每个线程独立算自己的目标地址、按元素搬运，硬件多线程掩盖访存延迟。

**练习 2**：`gather_v2` 的 def 与 infershape 放在 `op_host/` 根下，tiling 却在 `op_host/arch35/` 下，为什么？

**答案**：shape 推导与芯片无关（输出 shape 只由输入 shape 与 axis 决定），全芯片共用一份；tiling 结果依赖 UB 容量、核数、可用指令集，950 系列与其他芯片差异大，故按架构下沉，CMake 按目标芯片选择编哪份。

### 4.6 vfusion 类：multi_scale_deformable_attn_function 的多芯片分支

#### 4.6.1 概念说明

vfusion（特定模型融合算子）大类只有 7 个算子，都是「把某个模型的一段子图固化成单个算子」的产物，如 Deformable DETR 的多尺度可变形注意力、DETReg 的 modulate、YOLO 系的 scaled_masked_softmax。它们的特点是：计算逻辑 bespoke（无通用公式可套）、但性能敏感，所以同一算子常按芯片代际维护**多套完整实现**，编译期用宏直接选其一。

#### 4.6.2 核心流程

```
multi_scale_deformable_attn_function.cpp（唯一入口）
  #if __CCE_AICORE__ == 200        ← 310P（CCE 架构 200）
      → ms_deform_attn_310p.h      （KernelMultiScaleDeformableAttn310P<float>）
  #else                            ← 910B/950 等新一代
      → ms_deform_attn_generic.h   （通用正确性实现）
      → ms_deform_attn_high_perf.h （高性能实现）
  入口内按 TILING_KEY_IS(...) 在多组模板实例间分发
```

#### 4.6.3 源码精读

入口文件的条件包含与分发：

[vfusion/multi_scale_deformable_attn_function/op_kernel/multi_scale_deformable_attn_function.cpp:L16-L40](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/vfusion/multi_scale_deformable_attn_function/op_kernel/multi_scale_deformable_attn_function.cpp#L16-L40)

`#if __CCE_AICORE__ == 200` 包含 310P 实现，否则同时包含 generic 与 high_perf 两套。入口函数用 `GET_TILING_DATA(tilingData, tiling)` 读取 tiling（注意这里是宏展开形式，非 add_example 的 `GET_TILING_DATA_WITH_STRUCT`），再串行 `TILING_KEY_IS(...)` 分发：

[vfusion/multi_scale_deformable_attn_function/op_kernel/multi_scale_deformable_attn_function.cpp:L31-L63](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/vfusion/multi_scale_deformable_attn_function/op_kernel/multi_scale_deformable_attn_function.cpp#L31-L63)

`TILING_KEY_IS(1002)`、`TILING_KEY_IS(1004)`、`TILING_KEY_IS(1008)`、`TILING_KEY_IS(2002)`…… 每个 key 对应 `KernelMultiScaleDeformableAttnOpt<2, 16>` 这类**双非类型模板参数**实例——两个模板参数编码了关键形状特征（如采样点组数与向量化宽度），key 的千位大致对应一组、个位对应向量化档位。这是 u4-l2「tiling key 是二进制选择器」在真实融合算子上的规模化应用。

同目录的 `ms_deform_attn_310p.h` / `ms_deform_attn_generic.h` / `ms_deform_attn_high_perf.h` 三个平级文件就是「按芯片代际 × 性能档位」的完整实现矩阵——generic 保正确性先行、high_perf 逐步替换，这种「先让算子跑对，再分芯片跑快」的双轨策略是开发新融合算子的推荐路径。

#### 4.6.4 代码实践

1. **实践目标**：读懂 tilingKey 到模板实例的映射表。
2. **操作步骤**：列出入口文件 L31-L63 中全部 `TILING_KEY_IS` 分支及其模板参数；再在 `op_host/multi_scale_deformable_attn_function_tiling.cpp` 中 grep 对应 key 常量的定义与赋值条件（如采样点数、im2col 参数满足什么关系选 1002）。
3. **需要观察的现象**：key 的取值不是任意的，形如 `k*1000 + v`，`v` 与模板第二参数（16/32…）对应。
4. **预期结果**：产出一张「key → 模板参数 → 适用 shape 条件」映射表。

#### 4.6.5 小练习与答案

**练习**：为什么 vfusion 算子愿意为同一逻辑维护 generic 与 high_perf 两套实现？

**答案**：融合算子逻辑复杂，一次性写高性能版本风险高。generic 版本保证全 shape 正确、尽早可用；high_perf 针对主流 shape 区间用 tilingKey 圈定适用范围逐步替换，出问题可回退。两套并存靠 key 分发隔离，是复杂算子演进的稳妥工程策略。

## 5. 综合实践

**任务：四算子横向对比笔记（本讲核心实践）**

从 norm、loss、index、vfusion 四类中各选一个算子（建议 rms_norm、cross_entropy_loss、gather_v2、multi_scale_deformable_attn_function，也可换成 layer_norm、ctc_loss_v3、embedding_dense_grad、scaled_masked_softmax_v2），完成一份对比笔记，维度如下：

| 对比维度 | 追问 |
| --- | --- |
| 算子语义 | 输出依赖多少输入元素？（逐元素 / 整行 / 按索引跳转 / 整个子图） |
| op_host 结构 | tiling 是单策略还是多模式？tilingKey 是位段编码还是大数常量？有没有 `_arch35` 变体与 TilingParse？ |
| op_kernel 结构 | 单文件还是 base/特化继承？还是多平级变体文件？入口用 TILING_KEY_IS 还是 if constexpr？ |
| 编程模型 | 传统 CopyIn-Compute-CopyOut？SIMT（`__simt_vf__`）？还是 AI CPU 标量 C++？ |
| 与 elementwise（add_example）的关键差异 | 用一句话概括 |

操作步骤：

1. 逐个打开四个算子的 `op_host/` 与 `op_kernel/` 目录清单，先数文件、起分类名。
2. 只精读每个算子的 tiling 主函数开头 50 行与 kernel 入口函数，其余文件读注释与类名即可。
3. 填表并回答收尾问题：如果接到需求「实现一个 logit 时序掩码融合算子」，你会先去哪类找参考、借用它的哪一层？

参考结论（供对照）：掩码+softmax 形态最接近 vfusion 的 scaled_masked_softmax_v2（借它的 kernel 骨架与 tiling），shape 推导可借 index 类对掩码下标的处理，tiling 注册可借 rms_norm 的多模式框架。

## 6. 本讲小结

- 仓库共 14 个算子大类、约 540 个算子目录，`docs/zh/op_list.md` 是查算子能力（aclnn/图模式/硬件单元）的总清单；能力列 ✓/✗ 与目录缺失一一对应。
- 大类 CMakeLists 用 file(GLOB) 自动收集带构建脚本的子目录，新增算子目录无需登记，这是 `--ops` 单算子编译能自动解析依赖的基石。
- norm 类（rms_norm）代表归约型算子：行×列两维 tiling、按 shape/芯片选四种模式（NORMAL/SPLIT_D/MERGE_N/SINGLE_ROW），tilingKey 用位段加权编码，kernel 按模式拆多个平级文件。
- loss 类（cross_entropy_loss）代表多输入+可选输出算子：attr（reduction 等）参与 shape 推导，kernel 采用 base/特化两层继承，FP16/BF16 走「升 float 计算」精度套路。
- index 类（gather_v2）代表不规则访存算子：AI CPU 标量实现与 AI Core arch35 SIMT（`__simt_vf__` + LAUNCH_BOUND）实现并存，tilingKey 用大数常量区分六种数据布局变体。
- vfusion 类（multi_scale_deformable_attn_function）代表融合算子：`__CCE_AICORE__` 宏按芯片代际选实现，generic/high_perf 双轨演进，TILING_KEY_IS 分发到双模板参数实例。

## 7. 下一步学习建议

本讲完成了算子版图巡礼，u6 单元到此收官。接下来两个方向任选：

1. **进入测试体系（u7-l1、u7-l2）**：本讲多个实践都标注「待本地验证」，u7 将讲 UT/ST 框架——学会用 `build.sh -u` 跑 infershape/tiling UT 后，可以回头把 rms_norm 模式推演、gather_v2 key 映射等结论全部验证落地。
2. **按需深读**：若你的工作与某一大类强相关（如做大模型推理优先 norm/matmul），用本讲的「入口 → tiling → kernel → 搬运」阅读框架，挑该大类中 tiling 文件最长的算子精读——通常那就是该类里策略最丰富、最值得学的样本。
