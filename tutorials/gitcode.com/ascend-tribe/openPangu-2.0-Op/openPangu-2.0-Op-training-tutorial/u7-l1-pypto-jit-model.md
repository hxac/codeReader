# pypto 编程模型：Python DSL 写设备算子

## 1. 本讲目标

前六个单元里，我们接触的所有算子都是用 Ascend C（C++）写的：一个算子要拆成 `_def.cpp`、`_tiling.cpp`、`op_kernel/*.cpp`、`op_api/` 若干层文件，还要经过 CMake 挂接、run 包编译安装才能用（见 u1-l2、u1-l4）。本讲换一条完全不同的路线——**pypto**：直接用 Python 写设备侧 kernel。

读完本讲，你应该能够：

1. 说清 pypto 与 Ascend C 两条算子开发路线在**仓库组织、部署方式、抽象层次**上的差异，并能画出 pypto「Python 前端 → 设备代码」的调用链路。
2. 读懂 `@pypto.frontend.jit` 装饰的 kernel 签名：用 `pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16)` 类型标注声明张量契约，区分输入张量、输出张量与标量参数。
3. 掌握组织设备侧数据流的三原语 `pypto.view` / `pypto.cast` / `pypto.assemble`，以及 `mul/div/add/sub/round/clip/sum` 等计算原语的用法。
4. 理解 `pypto.set_vec_tile_shapes`、`pypto.loop_unroll`、`runtime_options`、`pass_options` 这些**切分与编译控制手段**如何影响生成的设备代码。
5. 会写 Host 侧封装函数（wrapper），把 `torch.empty` 分配的输出与 `torch` 张量按位置传给 kernel。

本讲的解剖标本是仓库中唯一一组 pypto 算子：QAT 量化家族中的 `ai_infra_qat_asymmetric_per_group`（非对称分组量化，前向 + 反向）。

## 2. 前置知识

本讲默认你已读过 u1-l2（昇腾自定义算子四层模型）。在此基础上补充几个概念：

- **DSL（Domain-Specific Language，领域专用语言）**：为某个特定领域量身定做的编程语言。pypto 就是「昇腾 NPU 向量算子」这个领域的 Python DSL——你写的是 Python 语法的代码，但它描述的是设备上逐 tile 执行的向量指令序列，而不是在 CPU 上解释执行的 Python。
- **JIT（Just-In-Time，即时编译）**：代码在运行期（而非提前编译期）被翻译成机器码。`@pypto.frontend.jit` 装饰器把一个 Python 函数登记为设备算子，由 pypto 框架在合适的时机编译成 NPU 设备代码。
- **tile（块）**：NPU 的向量指令一次处理一整块元素而不是单个元素。把大张量切成小块逐块处理，是设备算子的基本写法。这与 u2-l3 讲的 Host 侧 Tiling 概念同源，但 pypto 把切分直接写进 kernel 代码里（4.4 节）。
- **BF16 / FP32**：BF16（bfloat16）是 8 位指数 + 7 位尾数的半精度浮点，动态范围与 FP32 相同但精度低得多。因此本仓库的 QAT 算子全部采用「BF16 输入输出、FP32 内部计算」的策略，中间计算先 `cast` 上抛到 FP32。
- **STE（直通估计器）**：`round`、`clip` 这类操作不可导，量化感知训练用 STE 近似梯度。本讲只关注前向数值与编程模型，STE 的梯度推导留给 u7-l2 / u7-l3。

还有一个必须先说清楚的事实：**pypto 框架（编译器）本身的源码不在本仓库里**。仓库的 `pypto/` 目录只包含用这门 DSL 写的算子代码（`op_code/`）、文档（`docs/`）和测试（`tests/`）；`import pypto` 依赖的是训练环境中预先安装的外部 pypto 包（安装方式仓库内未见记载，待确认）。所以本讲对框架内部行为（编译时机、指令生成的细节）的描述，凡属推断都会明确标注。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py) | 本讲主标本。558 行，包含 6 个 `@pypto.frontend.jit` kernel（非对称分组前/反向、对称 per_channel 前/反向、对称 per_tensor 前/反向）和 6 个对应的 Host 侧 wrapper 函数 |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md) | 接口文档：三对算子的功能描述、接口签名、参数表、算法公式（LaTeX）、约束条件与支持规格（A2/A3、BF16 I/O + FP32 内部计算） |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/utils.py) | ST 公共工具：造数（多种分布）、CPU/FP64 golden、MARE/MERE/RMSE 精度对比（u7-l4 精讲） |
| [pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py) | 非对称前向 ST 用例：展示 pypto 算子的完整消费方式（import 即用）与 torch golden 写法 |

目录组织与 ascendc 侧的对照（各层的 `.gitkeep` 占位文件表明这是预留的目录规范）：

```text
pypto/src/ops-nn/quant/ai_infra_pypto_qat/
├── op_code/ai_infra_pypto_qat.py   # 全部算子实现：kernel + wrapper 一个文件搞定
├── docs/qat_ops.md                 # 接口文档（对标 ascendc 的 README + docs）
└── tests/                          # 测试（对标 ascendc 的 tests/ut + tests/st）
    ├── utils.py
    └── st/test_*.py                # 6 个 ST 用例，前反向 × 三种量化粒度
```

对比 ascendc 算子的五件套（README、docs、op_host、op_kernel、tests）：pypto 算子**没有 op_host / op_kernel / op_api 之分**——原型声明、切分、设备实现、对外接口全部收敛在一个 `.py` 文件里。

## 4. 核心概念与源码讲解

### 4.1 pypto 是什么：Python 前端 → 设备代码的链路

#### 4.1.1 概念说明

pypto 是「Python tensor operator」的编程模型：**用带类型标注的纯 Python 函数描述设备侧向量算子**，由 `@pypto.frontend.jit` 装饰器交给 pypto 框架编译成 NPU 设备代码。它要解决的问题和 Ascend C 一样（把算法落到 NPU 上），但抽象层次高得多：

| 维度 | Ascend C 路线（ascendc/） | pypto 路线（pypto/） |
| --- | --- | --- |
| 语言 | C++（Ascend C 方言） | Python DSL |
| 一个算子的文件数 | 3~4 层：`_def.cpp` / `_tiling.cpp` / `op_kernel/*.cpp` / `op_api/` | 1 个 `.py`（kernel + wrapper） |
| 原型注册 | `OpDef` 类 + `OP_ADD` 宏 | kernel 签名的类型标注（4.2 节） |
| 切分（tiling） | Host 侧独立 `_tiling.cpp`，产出 TilingData/tilingKey | kernel 内 `loop_unroll` + `set_vec_tile_shapes`（4.4 节） |
| 对外接口 | aclnn 两段式 C 接口，需编译 run 包安装 | 普通 Python 函数，`import` 即用 |
| 与 torch 的桥接 | 需要 torch_ops_extension 的 csrc 适配层（u6 单元） | wrapper 直接收发 `torch.Tensor` |
| 适用场景 | 大型融合算子（Attention 等 Cube+Vector 混合） | 轻量向量算子（本仓库：QAT 量化） |

注意最后两行的深层含义：pypto 算子被调用时，**传入的就是 torch 张量本身**——不需要 aclTensor 转换、不需要 dlopen 符号解析、不需要安装 run 包。ST 测试用最朴素的 `from op_code.ai_infra_pypto_qat import ai_infra_qat_asymmetric_per_group` 就完成了算子加载（[tests/st/test_ai_infra_qat_asymmetric_per_group.py:L13-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L13-L15)），这行 import 就是全部的「部署」。

#### 4.1.2 核心流程

一帧 pypto 算子调用的完整链路：

```text
┌─ Host 侧（Python）──────────────────────────────────────────┐
│  wrapper 函数                                                │
│   ├─ torch.empty 分配输出张量（输出也由调用方准备）            │
│   ├─ tensor.view() 把张量整形成 kernel 期望的分组形状          │
│   └─ kernel(*inputs)  ← 按位置传 torch 张量 + Python 标量      │
│         │                                                    │
│         ▼                                                    │
│  @pypto.frontend.jit 装饰的 kernel 函数                       │
│   ├─ 前端：解析签名里的 pypto.Tensor 类型标注（张量契约）        │
│   ├─ 中端：编译 pass（pass_options，如 vec_nbuffer_setting）   │
│   └─ 后端：生成 NPU 向量指令序列（设备代码）                    │
│         │                                                    │
└─────────│────────────────────────────────────────────────────┘
          ▼
┌─ Device 侧（NPU）───────────────────────────────────────────┐
│  按 loop_unroll 展开的循环，逐 tile 执行：                     │
│   view 取块 → cast 升精度 → 向量原语计算 → cast 降精度 → assemble 写回 │
└──────────────────────────────────────────────────────────────┘
```

图中「前端/中端/后端」的划分是按编译器的通用结构做的推断（框架源码不在仓库内，编译发生在装饰时还是首次调用时待确认）；但 Host 侧 wrapper 的行为和 Device 侧 tile 循环的执行模式，都可以从仓库源码直接读出，正是本讲 4.2~4.4 节的内容。

#### 4.1.3 源码精读

先看文件的头部与第一个 kernel 的装饰器：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L11-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L11-L20)——`import pypto` 引入外部框架包；`@pypto.frontend.jit(...)` 装饰器接受两个字典参数：

```python
@pypto.frontend.jit(
    runtime_options={
        "stitch_function_max_num": 64,
    },
    pass_options={"vec_nbuffer_setting": {-1: 2, -2: 1}},
)
def ai_infra_qat_asymmetric_per_group_kernel(...):
```

- `runtime_options`：**运行期选项**。`stitch_function_max_num=64` 从命名推断是限制「函数拼接（stitch）」优化的规模上限——即编译器最多把多少个函数缝合进一次设备调用（本仓库 6 个 kernel 全部设为 64，具体语义在框架内，待确认）。
- `pass_options`：**编译 pass 选项**。`vec_nbuffer_setting` 是向量指令的 n-buffer（多缓冲）配置，键 `-1`/`-2` 推测是按指令类别的通配键，值 `2` 表示双缓冲、`1` 表示单缓冲。双缓冲的经典收益是「数据搬运与计算重叠」的流水化。注意不同 kernel 的取值不同：非对称前/反向与 per_channel 反向用 `{-1: 2, -2: 1}`（L19、L105、L302），per_tensor 反向用 `{-1: 4}`（L457），per_channel 前向与 per_tensor 前向则完全不传 `pass_options`（L244、L398）——**这些选项是按算子的数据依赖特点逐个调优的**，改它们会改变生成的指令缓冲组织，进而影响性能与 UB 占用。

观察一个有趣的对照：全文件 6 处装饰器，`runtime_options` 完全一致、`pass_options` 三种取值。这说明 `pass_options` 才是性能调优的主旋钮。

#### 4.1.4 代码实践

**实践目标**：建立「一个文件装下一个算子」的直观感受，并确认 pypto 框架的边界。

**操作步骤**：

1. 统计 `ai_infra_pypto_qat.py` 中的 kernel 与 wrapper 数量：

   ```bash
   grep -n "^def \|^@pypto.frontend.jit" \
     pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py
   ```

2. 对比 ascendc 侧同功能粒度的目录：`ls ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/`，数一数它有几个子目录、几个源文件。
3. 在仓库根目录执行 `grep -rn "import pypto" --include="*.py" . | grep -v ai_infra_pypto_qat`，确认除本算子目录外没有其他地方引用 pypto。

**需要观察的现象**：第 1 步应输出 6 个 `@pypto.frontend.jit` 与 12 个 `def`（6 个 `*_kernel` + 6 个 wrapper）；第 3 步应没有任何输出——pypto 在本仓库的使用范围就这一个目录。

**预期结果**：ascendc 的 aggregate_hidden 需要 op_host（2 个文件）+ op_kernel（3 个文件）+ tests 才能表达一个前向；pypto 用约 100 行表达一对前反向。两条路线的复杂度差距来自抽象层次，而不是算子本身简单（QAT 的公式链并不短，见 4.3 节）。

### 4.2 kernel 签名与类型标注：参数契约 + Host 侧 wrapper

#### 4.2.1 概念说明

pypto kernel 的函数签名承担了 Ascend C 路线里 `_def.cpp` 原型注册的职责——**类型标注就是张量契约**：

```python
weight: pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16)
```

这个标注声明了三件事：该参数是张量、期望的形状约束、期望的数据类型。其中：

- `pypto.DYNAMIC`：该维度编译期不定，运行期由实际传入的张量决定。kernel 内部可以用 `weight.shape[0]`、`weight.shape[1]` 读到真实值（L34-L35、L257）。
- 列表里的 `...`（Python 的 Ellipsis）：表示其余维度不做逐个声明（具体语义由 pypto 框架定义，仓库内无框架源码，待确认）。
- `[pypto.DYNAMIC, 1]`：二维张量，第二维编译期固定为 1（列向量）。
- `[1, 1]`：完全固定的标量形状（per_tensor 的 scale，L403）。
- **没有类型标注的参数**（如 `eps, n_levels, neg_clip_val, clip_val, shift`）：Python 标量，在 Host 侧预计算后按值传入，角色相当于 ascendc 路线里走 Attr 通路的标量属性（u2-l5）。

另一个关键设计是**目标传递风格（destination-passing style）**：输出张量不在返回值里，而是作为普通参数写进签名，由调用方先 `torch.empty` 分配好再传入，kernel 用 `assemble` 往里写。对比 PyTorch 习惯的 `y = f(x)`，这里是 `f(x, y)`。好处是输出内存的分配策略（shape、dtype、内存池）完全掌握在 Host 侧 wrapper 手里，设备代码只管算。

#### 4.2.2 核心流程

Host wrapper 的固定套路（五步）：

```text
1. Host 预计算标量：n_levels = 2**(bit-1)、shift = 0.5、neg_clip_val = -clip_val
2. torch.empty 分配输出（shape/dtype/device 与输入对齐）
3. tensor.view() 把 (N, M) 折叠成 (num_groups, group_size) 的分组视图（零拷贝）
4. 按签名顺序组装 inputs 列表：[输入张量..., 输出张量..., 标量...]
5. kernel(*inputs) 一次调用；wrapper 返回前把分组视图 view 回原始形状
```

参数顺序是**位置契约**：`inputs` 列表的第 i 个元素必须对应 kernel 签名的第 i 个形参。这与 u1-l2 讲的「四层靠算子名与参数顺序对齐」是同一条纪律在 pypto 里的翻版。

#### 4.2.3 源码精读

非对称前向 kernel 的完整签名：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L21-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L21-L31)——9 个参数分三类：

```python
def ai_infra_qat_asymmetric_per_group_kernel(
    weight: pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16),   # 输入：分组视图 (G, group_size)
    scale:   pypto.Tensor([pypto.DYNAMIC, 1], pypto.DT_BF16),    # 输入：每组一个 scale (G, 1)
    offset:  pypto.Tensor([pypto.DYNAMIC, 1], pypto.DT_BF16),    # 输入：每组一个 offset (G, 1)
    output_bf16: pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16),  # 输出：与 weight 同形
    eps, n_levels, neg_clip_val, clip_val, shift                 # 标量：无标注
):
```

对应的 wrapper：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L77-L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L77-L100)——完整走一遍五步套路：

```python
def ai_infra_qat_asymmetric_per_group(weight, scale, offset, group_size=128, bit=4,
                                      eps=1e-4, clip_val=0.99):
    n_levels = 2 ** (bit - 1)          # ① Host 预计算标量
    shift = 0.5
    neg_clip_val = -clip_val

    output_bf16 = torch.empty(weight.shape, dtype=weight.dtype, device=weight.device)  # ② 分配输出
    weight_grouped = weight.view(-1, group_size)             # ③ 分组视图 (G, group_size)
    output_bf16_grouped = output_bf16.view(-1, group_size)

    inputs = [weight_grouped, scale, offset, output_bf16_grouped,   # ④ 按签名顺序组装
              eps, n_levels, neg_clip_val, clip_val, shift]
    ai_infra_qat_asymmetric_per_group_kernel(*inputs)        # ⑤ 位置传参调用
    return output_bf16_grouped.view(weight.shape)            #    view 回 (N, M)
```

三个细节值得咀嚼：

1. **`view(-1, group_size)` 是纯视图**：不搬数据，只是让 (N, M) 的内存按 `group_size` 重新解释成 (N·M/group_size, group_size)。kernel 签名里的 `[pypto.DYNAMIC, ...]` 接收的就是这个分组形状；`weight.shape[1]` 读到的 `group_size`（L35）正是从这里来的。
2. **输出也做分组视图**：`output_bf16_grouped` 与 `weight_grouped` 同构，kernel 侧对输出 tile 的 `assemble` 偏移才能与输入 `view` 偏移共用同一套 `[g_offset, 0]` 坐标。
3. **用户接口形状 ≠ kernel 接口形状**：wrapper 对外收发 (N, M)，对内喂 (G, group_size)。这是「接口形态适配」在 pypto 里最轻量的实现——一个 `view` 的事，对比 ascendc 路线要动 op_api 的 Pad/Transpose（u2-l5）。

反向 kernel 的签名同理，只是输出变成三路（[L111-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L111-L124)）：`grad_weight_out`、`grad_scale_out`、`grad_offset_out` 三个张量排在输入之后、标量之前，wrapper 也按同样顺序组装（[L224-L238](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L224-L238)）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「参数顺序契约」，体会签名与 inputs 列表的逐位对应。

**操作步骤**：

1. 打开 [op_code/ai_infra_pypto_qat.py:L459-L468](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L459-L468)（per_tensor 反向 kernel 签名），抄下 8 个形参的名字。
2. 再打开 [L545-L558](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L545-L558)（对应 wrapper），抄下 `inputs` 列表里 8 个元素的来源表达式。
3. 画一张两列对照表：形参名 ↔ inputs 元素 ↔ 该元素是 `torch.empty` 新分配 / 输入透传 / Host 计算的标量。
4. 假设把 wrapper 里 `inputs` 列表的 `grad_weight_out` 与 `grad_scale_out` 交换位置，只做**纸面推演**（不要改源码）：写出 kernel 内哪些计算会写到错误的张量里。

**需要观察的现象**：对照表中每个 kernel 形参都恰好有一个 inputs 元素对应；两个输出张量交换后，`assemble(grad_weight_tile, ...)`（L516）的落点会变成 `grad_scale_out` 的内存，但 kernel 浑然不觉——因为它是按位置接收的。

**预期结果**：8 个参数 = 3 个输入透传（grad_output/weight/scale）+ 2 个新分配输出（grad_weight_out/grad_scale_out）+ 3 个标量（eps/min_v/max_v）。纸面推演结论：位置失配不报错、只出错值，这类 bug 只能靠 ST 精度测试（u7-l4）抓出来。

**待本地验证**：第 4 步的推演在有 NPU + pypto 包的环境中可以做实验证实（改 wrapper 的列表顺序、跑 ST、观察输出 shape/数值错乱），本环境无法运行。

#### 4.2.5 小练习与答案

**练习 1**：`ai_infra_qat_asymmetric_per_group_kernel` 的 9 个参数里，哪个是输出张量？判断依据是什么？

> **答案**：`output_bf16`（L25）。直接依据是它是全函数唯一被 `pypto.assemble` 作为落点写入的张量（L74）；命名上 `_out` / `_bf16` 后缀也是全文件一致的输出标记（对比反向 kernel 的 `grad_weight_out` 等）。

**练习 2**：per_group 的 `scale` 标注为 `[pypto.DYNAMIC, 1]`，per_tensor 的 `scale` 却标注为 `[1, 1]`（[L403](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L403)）。为什么？

> **答案**：per_group 的 scale 有 \(G = N \times M / \text{group\_size}\) 个、per_channel 有 N 个，组数/通道数要到运行期才知道，所以首维必须是 DYNAMIC，次维固定为 1（列向量）；per_tensor 全部元素共享一个 scale，形状就是编译期确定的标量 (1, 1)，无需 DYNAMIC。这与 docs 中三种算子的适用场景（Embedding / Lm Head / Linear）一一对应（[docs/qat_ops.md:L501-L510](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L501-L510)）。

**练习 3**：wrapper 为什么必须由自己 `torch.empty` 分配输出，而不是让 kernel 返回结果？

> **答案**：目标传递风格。输出张量写进签名后，kernel 的设备代码只负责向给定内存写数；分配时机、shape/dtype/device 策略全在 Host 侧 wrapper 掌控，且签名里的输出标注（`[pypto.DYNAMIC, ...], DT_BF16`）本身就是 kernel 与 wrapper 之间关于输出形态的显式契约。

### 4.3 数据流三原语：view / cast / assemble 与计算原语

#### 4.3.1 概念说明

pypto kernel 的函数体就是一个「取块 → 计算 → 写回」的循环，三个原语撑起整个数据流：

| 原语 | 方向 | 语义 | 对标的 torch 写法 |
| --- | --- | --- | --- |
| `pypto.view(t, [tile_shape], [offsets])` | GM → 寄存器 | 从全局张量 `t` 的 `offsets` 位置切一个 `tile_shape` 的块 | `t[o0:o0+r, o1:o1+c]` |
| `pypto.cast(t, dtype)` | 寄存器 → 寄存器 | 数据类型转换（升精度 / 降精度） | `t.to(dtype)` |
| `pypto.assemble(tile, [offsets], out)` | 寄存器 → GM | 把算好的块写回输出张量 `out` 的 `offsets` 位置 | `out[o0:o0+r, o1:o1+c] = tile` |

`view` 与 `assemble` 是一对**镜像**操作：同一个 `[g_offset, 0]` 偏移，前者读输入、后者写输出。注意到没有显式的「DataCopy」——数据搬运被这对原语吸收了，这是 DSL 比 Ascend C 高一档抽象的地方（对比 u2-l4 的 CopyIn/CopyOut/TPipe/TQue 全套机制）。

计算原语则是一组无副作用的向量运算：`mul` / `div` / `add` / `sub`（四则）、`maximum`（逐元素取大，兼做防零保护）、`abs`、`round(x, decimals=0)`（指定位数四舍五入）、`clip(x, lo, hi)`（截断）、`sum(x, dim=..., keepdim=...)`（沿轴归约）、`where(cond, a, b)`（二选一）、`full(shape, val, dtype)`（造常量张量）、`expand_clone(t, shape)`（广播扩克隆）。API 风格刻意贴近 torch，torch 用户几乎零成本迁移。

#### 4.3.2 核心流程

非对称前向 kernel 的计算链是 LSQ+ 量化的完整公式（docs 的 Step 1~6，[docs/qat_ops.md:L566-L620](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L566-L620)）。设 \(s'\) 为防零保护后的 scale、\(\alpha = s' n_{\text{levels}}\)、\(n_{\text{levels}} = 2^{\text{bit}-1}\)、shift 固定为 0.5：

\[
\begin{aligned}
s' &= \max(s,\ \varepsilon) \\
\alpha &= s' \cdot n_{\text{levels}} \\
w_{\text{shifted}} &= w - \text{offset} \\
w_{\text{norm}} &= w_{\text{shift}} \,/\, \alpha \\
w_{\text{clipped}} &= \mathrm{clip}(w_{\text{norm}},\ -c,\ c) \\
w_{\text{quant}} &= w_{\text{clipped}} \cdot n_{\text{levels}} - 0.5 \\
w_{\text{rounded}} &= \mathrm{round}(w_{\text{quant}}) \\
w_{\text{out}} &= \frac{w_{\text{rounded}} + 0.5}{n_{\text{levels}}} \cdot \alpha + \text{offset}
\end{aligned}
\]

直觉上这是一趟「减 offset 搬家 → 归一化到 [-c, c] → 量化到整数格点 → 再搬回来」的往返：任何能被量化网格精确表示的权重，往返后数值不变；不能表示的则落到最近的格点上——量化噪声由此注入训练。

一个容易混淆的点：**设备 kernel 里没有 detach**。`round` 在 kernel 里就是朴素的 `pypto.round`（L64）；STE 的 detach 技巧出现在 torch 侧 golden 里（ST 测试 [L66](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L66) 的 `(weight_clipped.round() - weight_clipped).detach() + weight_clipped`）。原因：前向 kernel 只需要**数值**；梯度怎么流是另一个独立 kernel（`*_backward_kernel`）用掩码显式编码的事（u7-l3 精讲）。pypto 路线把「前向数值」与「反向梯度」拆成两个设备函数，而不是像 torch 那样挂在同一张自动求导图上。

#### 4.3.3 源码精读

kernel 体内的数据流（循环骨架见 4.4 节，这里看单次迭代）：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L46-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L46-L53)——取块与升精度：

```python
weight_tile = pypto.view(weight, [tile_groups, group_size], [g_offset, 0])
weight_fp32 = pypto.cast(weight_tile, pypto.DT_FP32)

scale_tile = pypto.view(scale, [tile_groups, 1], [g_offset, 0])
offset_tile = pypto.view(offset, [tile_groups, 1], [g_offset, 0])

scale_fp32 = pypto.cast(scale_tile, pypto.DT_FP32)
offset_fp32 = pypto.cast(offset_tile, pypto.DT_FP32)
```

四个张量都在行偏移 `g_offset` 处切块；`(tile_groups, 1)` 的 scale/offset 块与 `(tile_groups, group_size)` 的 weight 块行数相同，逐行广播。

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L55-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L55-L74)——公式到代码的一一对应：

```python
protected_scale = pypto.maximum(scale_fp32, eps)     # s' = max(s, ε)
alpha = pypto.mul(protected_scale, n_levels)          # α = s'·n_levels

weight_shifted = pypto.sub(weight_fp32, offset_fp32)  # w − offset
weight_norm = pypto.div(weight_shifted, alpha)        # / α
weight_clipped = pypto.clip(weight_norm, neg_clip_val, clip_val)   # clip

weight_scaled = pypto.mul(weight_clipped, n_levels)   # × n_levels
weight_shifted2 = pypto.sub(weight_scaled, shift)     # − 0.5
weight_rounded = pypto.round(weight_shifted2, decimals=0)          # round

weight_unshifted = pypto.add(weight_rounded, shift)   # + 0.5
weight_denorm = pypto.div(weight_unshifted, n_levels) # / n_levels
weight_rescaled = pypto.mul(weight_denorm, alpha)     # × α
output = pypto.add(weight_rescaled, offset_fp32)      # + offset

output_tile = pypto.cast(output, pypto.DT_BF16)       # 降精度
pypto.assemble(output_tile, [g_offset, 0], output_bf16)             # 写回 GM
```

逐行对照 4.3.2 的公式：8 条公式 → 13 条语句（乘除加减各占一行、clip 单列、round 带命名参数 `decimals=0`），最后一行 `assemble` 用与开头 `view` **完全相同的 `[g_offset, 0]`** 偏移写回输出——读写镜像闭环。标量 `eps/n_levels/clip_val/shift` 直接与张量混合运算，DSL 负责把它们广播成向量操作数。

再欣赏一个反向 kernel 里的精妙技巧——**用数值方法而不是分支生成 0/1 掩码**：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L163-L174](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L163-L174)：

```python
diff = pypto.sub(weight_norm, weight_clipped)   # 截断处非 0，界内为 0
abs_diff = pypto.abs(diff)
big_number = 1e15
sign = pypto.mul(abs_diff, big_number)          # 非 0 → 巨大
is_out = pypto.clip(sign, 0.0, 1.0)             #   → clip 成 1；0 → 仍是 0
one = pypto.full(is_out.shape, 1.0, is_out.dtype)
mask_f32 = pypto.sub(one, is_out)               # mask = 1 − is_out
```

原理：`weight_clipped` 是 `weight_norm` 截断后的值，二者相减在「界内」处恒为 0、在「越界」处非 0；乘以 \(10^{15}\) 放大再 clip 到 \([0,1]\)，就把任意非零差值规整成精确的 1.0。于是 `mask` 是干净的 0/1 浮点掩码，后续梯度乘掩码全用向量乘法完成，全程无分支、无比较指令。docs 的「关键实现细节」一节记载了同一思路（[docs/qat_ops.md:L792-L800](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L792-L800)）。per_channel/per_tensor 反向里还有一个更简的版本（先 `abs` 后直接 clip，L348-L352），因为 `round` 后的值是整数格点、差值天然 ≥ 1。

#### 4.3.4 代码实践

**实践目标**：验证「公式 ↔ 代码 ↔ golden」三方一致，把 4.3.2 的公式变成可运行的参照。

**操作步骤**：

1. 写一个独立的 CPU 参考脚本（**示例代码**，非仓库原有）：

   ```python
   import torch

   def asymmetric_qat_golden(weight, scale, offset, group_size=128, bit=4,
                             eps=1e-4, clip_val=0.99):
       w2d = weight.float().view(-1, group_size)
       s = scale.float()
       o = offset.float()
       n_levels = 2 ** (bit - 1)
       protected = torch.maximum(s, torch.tensor(eps))
       alpha = protected * n_levels
       norm = (w2d - o) / alpha
       clipped = torch.clamp(norm, -clip_val, clip_val) * n_levels - 0.5
       rounded = torch.round(clipped)          # 前向数值，无需 detach
       out = ((rounded + 0.5) / n_levels) * alpha + o
       return out.view(weight.shape).to(torch.bfloat16)
   ```

2. 用小规模数据自检往返性：造一组已经落在量化格点上的权重（例如先随机量化再反量化得到的 `weight`），检查 `asymmetric_qat_golden` 输出与输入逐元素相等。
3. 打开 ST 测试里的官方 golden（[tests/st/test_ai_infra_qat_asymmetric_per_group.py:L18-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/tests/st/test_ai_infra_qat_asymmetric_per_group.py#L18-L77)），逐行比对你写的版本与它的差异（它多了 shape 校验、`is_golden` 双精度模式与 STE detach）。

**需要观察的现象**：第 2 步中，格点上的权重往返后完全不变（量化是恒等映射）；随机连续权重则有最多半格点的偏差。

**预期结果**：你的 golden 与官方 golden 的数值路径一致（`is_golden=False` 分支），说明你已经能从 kernel 源码独立复原算法——这正是给 NPU 实现做精度对照的前提。

**待本地验证**：本实践只涉及 CPU 上的 torch，可直接运行；与 NPU kernel 输出的对比需要在有 pypto 包的环境里做（u7-l4 会用 `tests/utils.py` 的 `forward_test` 完成这一步）。

#### 4.3.5 小练习与答案

**练习 1**：`view` 与 `assemble` 的偏移参数都是 `[g_offset, 0]`，第二维为什么恒为 0？

> **答案**：分组视图 (G, group_size) 里，每个 tile 一次吃下整组（列方向覆盖整个 `group_size`），所以列偏移永远是 0；循环只沿组方向（第 0 维）推进。同理 per_channel kernel 里偏移是 `[n_offset, 0]`（L281）——tile 整行处理，只有行偏移在动。

**练习 2**：为什么所有 kernel 都先 `cast` 到 FP32 算完再 `cast` 回 BF16，而不是全程 BF16？

> **答案**：BF16 只有 7 位尾数（约 3 位十进制有效数字），而量化链里有除法、`round` 到整数格点这类对精度敏感的操作，BF16 直接算会把误差放大到不可接受。docs 的支持规格明确写着「BF16（输入/输出），FP32（内部计算）」（[docs/qat_ops.md:L648](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L648)）。这也解释了标量为什么以 Python float 传入：参与的是 FP32 中间计算。

**练习 3**：`pypto.full` 在 L170 出现了 `is_out.shape` 作为形状参数。这说明了什么？

> **答案**：`full` 的 shape 参数可以取自另一个设备张量的 `.shape`，即在设备侧动态地按当前 tile 的形状造常量张量（这里造了一个全 1 的张量用来算 `1 − is_out`）。配套的还有 `expand_clone`（L428，把 (1,1) 的 scale 克隆扩成 (tile_n, 1) 实现广播）——两者都是「形状在设备侧才知道」时的造数手段。

### 4.4 切分与调度控制：loop_unroll、set_vec_tile_shapes 与编译选项

#### 4.4.1 概念说明

 ascendc 路线里，数据怎么切是 Host 侧 tiling 函数的职责，产出 TilingData 下发设备（u2-l3）。pypto 把这件事**内嵌进 kernel 代码**，用两个调用表达：

- **`pypto.set_vec_tile_shapes(rows, cols)`**：设定向量指令一次处理的 tile 形状（行数 × 列数）。它决定了每条向量指令搬多少数据、UB 里同时驻留多大的块——本质就是「迷你 tiling」。
- **`pypto.loop_unroll(start, stop, step, name=, idx_name=, unroll_list=)`**：带「不完全展开」语义的循环。`unroll_list` 给出可用的块长度清单（从大到小），循环体通过解包拿到 `(偏移, 本轮块长)`；余量不够下一个大块时降级用小块收尾，保证任意 `stop` 都被整块 + 尾块精确覆盖。

再加上 4.1 节的 `runtime_options` / `pass_options`（编译期旋钮）与 `pypto.experimental.set_operation_options`（实验性算子选项），构成 pypto 的完整调度控制面。

#### 4.4.2 核心流程

以 per_channel 前向为例（[L260-L265](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L260-L265)），设 n=1000、`unroll_list=[512, 32, 8]`：

```text
迭代空间 [0, 1000) 的覆盖方案：
  512×1 + 32×15 + 8×1  =  512 + 480 + 8  =  1000   （共 17 次循环）

第 i 次循环解包得到 (n_offset, unroll_length)：
  (0, 512) (512, 32) (544, 32) ... (992, 8)
      │         │                    │
      ▼         ▼                    ▼
  大块满载   中块收尾             尾块精确对齐
  （512 行 tile 复用同一份 kernel 体，unroll_length 即 tile_n）
```

为什么不干脆 `for i in range(n)` 逐行处理？因为 tile 形状是性能的根源：大 tile 让向量指令满载、让 `scale_tile` 这类小张量的搬运被更多数据摊薄；而尾块必须存在，是因为 n 不一定整除 512。`unroll_list` 的取值是**算子形状特征调出来的经验值**：weight 是 (N, M) 大矩阵的三个 kernel 用 `[512, 32, 8]`（L264、L324、L489），分组视图 (G, group_size) 的两个非对称 kernel 用 `[512, 256]`（L33、L133），且 group_size 本身只会是 64/128/256（docs 约束，[L639](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L639)），尾块粒度不需要细到 8。

（块长选择的具体策略实现在 pypto 框架内，本仓库不可见；上面「大块优先、余量降级」是对 `unroll_list` 语义的合理推断，标注待确认。）

#### 4.4.3 源码精读

非对称前向 kernel 的循环骨架：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L32-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L32-L45)：

```python
pypto.experimental.set_operation_options(combine_axis=True)
unroll_list = [512, 256]
num_groups = scale.shape[0]        # 运行期从张量读形状
group_size = weight.shape[1]
pypto.set_vec_tile_shapes(128, 128)

for g_offset, unroll_length in pypto.loop_unroll(
    0, num_groups, 1,
    name="LOOP_GROUPS", idx_name="g_offset",
    unroll_list=unroll_list
):
    tile_groups = unroll_length    # 本轮 tile 的行数
    ...                            # 4.3 节讲解的循环体
```

四个要点：

1. **形状在设备侧读**：`scale.shape[0]`、`weight.shape[1]` 是运行期取值——DYNAMIC 标注的兑现处。对比 ascendc 路线：那里 shape 在 Host 侧 tiling 里读、经 TilingData 传下去；pypto 把这个闭环缩短成了两行。
2. **`set_vec_tile_shapes(128, 128)`**：非对称前向的向量 tile 是 128×128；对照其他 kernel——per_channel 前向 `(32, 512)`（L258）、per_channel 反向 `(4, min(m, 4096))`（L317-L318）、非对称反向 `(64, 128)`（L136）。**tile 形状按算子的归约模式逐个定制**：纯逐元素的前向偏爱方形块；反向要沿 M 维归约（`sum(dim=1)`），于是压成扁长条（4 行 × 最多 4096 列）。
3. **tile 形状可以在 kernel 中途改**：per_tensor 反向在循环内做完逐元素段后，切换成 `pypto.set_vec_tile_shapes(512, 1)` 再做沿 dim=0 的归约（[L530-L532](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L530-L532)）——同一段代码里，逐元素计算用面状 tile、跨行归约用列向量 tile。非对称反向则干脆把 `set_vec_tile_shapes(64, 128)` 放在循环体内每个迭代重设（L136）。可见它是**作用域化的调度参数**，不是全局一次性配置。
4. **`combine_axis=True`**：实验性选项，推断为允许编译器把相邻的小轴合并成大向量以提高指令满载率。全文件唯一一个 `combine_axis=False` 出现在 per_tensor 反向（L469）——恰是那个需要在循环外维护跨 tile 累加器的 kernel（见下），轴合并会改变 tile 的维度语义、干扰显式累加（推断，待确认）。

per_tensor 反向还展示了**跨 tile 累加器**模式——grad_scale 是全局标量，每个 tile 只算出部分和，必须跨迭代累加：

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L482-L483](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L482-L483)——循环**外**定义 FP32 累加器：

```python
# 在循环外初始化 FP32 的局部累加器
grad_scale_acc = pypto.full([1, 1], 0.0, pypto.DT_FP32)
```

[pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py:L535-L542](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L535-L542)——循环**内**用切片赋值累加、循环后写回：

```python
grad_scale_tile = pypto.add(grad_scale_mul_tile_n, grad_scale_div_tile_n)
# 将当前 Tile 的梯度累加到全局 FP32 寄存器中
grad_scale_acc[:] = pypto.add(grad_scale_acc, grad_scale_tile)
...
final_grad_scale_fp32 = pypto.mul(grad_scale_acc, scale_mask_fp32)
final_grad_scale_bf16 = pypto.cast(final_grad_scale_fp32, pypto.DT_BF16)
pypto.assemble(final_grad_scale_bf16, [0, 0], grad_scale_out)
```

`grad_scale_acc[:] = ...` 这种切片赋值语法让一个设备张量跨越 `loop_unroll` 的多个迭代保持活性并被更新——这是 pypto 表达「归约结果跨 tile 存活」的惯用法，等价于 ascendc kernel 里在 UB 上开一块跨迭代复用的缓冲（对比 u2-l4 的权重 fp32 预热缓冲）。最后统一乘 scale 掩码、降精度、`assemble` 到 `[0, 0]`（标量输出的唯一位置）。

汇总本仓库 6 个 kernel 的调度参数取值：

| kernel | set_vec_tile_shapes | unroll_list | combine_axis | pass_options |
| --- | --- | --- | --- | --- |
| 非对称前向 (L36) | (128, 128) | [512, 256] | True | {-1: 2, -2: 1} |
| 非对称反向 (L136) | (64, 128)（循环内） | [512, 256] | True | {-1: 2, -2: 1} |
| per_channel 前向 (L258) | (32, 512) | [512, 32, 8] | True | 无 |
| per_channel 反向 (L318) | (4, min(m,4096)) | [512, 32, 8] | True | {-1: 2, -2: 1} |
| per_tensor 前向 (L413) | (32, 512) | [512, 32, 8] | True | 无 |
| per_tensor 反向 (L473, L530) | (4, min(m,4096)) → (512, 1) | [512, 32, 8] | **False** | {-1: 4} |

#### 4.4.4 代码实践

**实践目标**：把「调度参数是按算子特征调优的」这一判断变成你自己的观察。

**操作步骤**：

1. 用 grep 列出所有调度调用及其行号：

   ```bash
   grep -n "set_vec_tile_shapes\|loop_unroll\|set_operation_options\|unroll_list" \
     pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py
   ```

2. 对每个 kernel 回答三个问题：输入形状是什么（分组视图还是原始 (N,M)）？循环里有没有 `sum(dim=...)` 归约？归约结果要不要跨 tile 累加？
3. 把答案填进 4.4.3 的表格，验证「有归约 → 扁长条 tile」「有跨 tile 累加 → combine_axis=False」「分组视图 → 粗尾块」这三条经验规律。
4. 纸面实验（不改源码）：如果把非对称前向的 `set_vec_tile_shapes(128, 128)` 改成 `(4, 16384)`，按 4.4.3 的规律推断性能会变好还是变坏，理由是什么？

**需要观察的现象**：规律应当全部成立。第 4 步的推断要点：非对称前向的 tile 是 (tile_groups, group_size)，group_size 最大 256，列方向 16384 远超单块数据量，向量指令无法满载，且 scale/offset 的 (tile_groups, 1) 小块搬运次数暴增——应推断为变坏。

**预期结果**：你得到一张与 4.4.3 表一致的调度参数矩阵，并归纳出「tile 形状跟随归约模式、unroll 粒度跟随尾块需求、缓冲配置跟随数据依赖」的调优直觉。

**待本地验证**：第 4 步的性能推断需要 NPU + pypto 环境实测（对比两种 tile 形状的耗时）才能确认。

#### 4.4.5 小练习与答案

**练习 1**：n=1000、`unroll_list=[512, 32, 8]` 时，循环各次迭代的 `(n_offset, unroll_length)` 序列是什么？

> **答案**：512×1、32×15、8×1，共 17 次迭代——首次 (0, 512)，随后 15 次 32 行的中块（偏移 512、544、…、960），最后 1 次 8 行的尾块 (992, 8)，合计 512+480+8=1000。

**练习 2**：`vec_nbuffer_setting` 设成 2（双缓冲）有什么收益？为什么 per_tensor 反向反而用 `{-1: 4}`？

> **答案**：双缓冲让「搬下一块数据」与「算当前块」重叠，是流水化的基本手段。per_tensor 反向有跨 tile 的 grad_scale 累加链，第 i 块的累加依赖第 i-1 块的结果，需要更深的缓冲来掩盖这条依赖链的延迟，于是加到 4 份（推断：缓冲深度匹配依赖链长度；`-1`/`-2` 键的确切含义与选择逻辑在框架内，待确认）。

**练习 3**：为什么 `set_vec_tile_shapes` 可以在 kernel 执行中途（L530）修改？这与 ascendc 的 tiling 有什么本质不同？

> **答案**：它是对「接下来这段计算」的向量指令形状声明，作用域随调用位置变化——逐元素段用 (4, m)，切到 dim=0 归约段就重设为 (512, 1)。本质不同在于：ascendc 的 tiling 是 Host 侧一次性算好、整体下发的静态契约（TilingData）；pypto 的切分是设备代码里的可编程语句，能在同一 kernel 内按计算阶段动态切换。

## 5. 综合实践：编写最小 pypto kernel——y = x·k + b

把本讲四个模块串起来：写一个元素级 `y = x·k + b` 的 pypto 算子，**严格仿照 `ai_infra_qat_asymmetric_per_group` 的签名风格**——张量带 `pypto.Tensor` 形状标注、标量裸参数、输出目标传递、循环体走 view → cast → 计算 → cast → assemble。

**实践目标**：独立产出一个结构完整（jit 装饰 + 类型标注 + tile 循环 + wrapper）的 pypto 算子，并用 CPU golden 验证算法理解。

**操作步骤**：

1. 在 `pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/` 之外**任选一个本地目录**创建 `ai_infra_scale_shift.py`（示例代码，非仓库原有，请勿写入仓库目录），内容如下：

   ```python
   import pypto
   import torch


   @pypto.frontend.jit(
       runtime_options={
           "stitch_function_max_num": 64,
       },
   )
   def ai_infra_scale_shift_kernel(
       x: pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16),
       y: pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16),
       k,
       b
   ):
       n, m = x.shape                       # DYNAMIC 形状在设备侧读取
       pypto.set_vec_tile_shapes(32, 512)   # 逐元素计算：参照 per_channel 前向的块形

       for n_offset, unroll_length in pypto.loop_unroll(
           0, n, 1,
           name="LOOP_N_UNROLL",
           idx_name="n_offset",
           unroll_list=[512, 32, 8]
       ):
           tile_n = unroll_length

           x_tile = pypto.view(x, [tile_n, m], [n_offset, 0])
           x_fp32 = pypto.cast(x_tile, pypto.DT_FP32)   # BF16 → FP32

           scaled = pypto.mul(x_fp32, k)                 # x·k（标量广播）
           out_fp32 = pypto.add(scaled, b)               # + b

           out_tile = pypto.cast(out_fp32, pypto.DT_BF16)
           pypto.assemble(out_tile, [n_offset, 0], y)    # 写回输出，偏移与 view 镜像

   def ai_infra_scale_shift(x, k=2.0, b=1.0):
       y = torch.empty(x.shape, dtype=x.dtype, device=x.device)   # 输出由 wrapper 分配
       inputs = [x, y, k, b]                                      # 顺序 = 签名顺序
       ai_infra_scale_shift_kernel(*inputs)
       return y
   ```

   对照检查五处仿写是否到位：装饰器只给 `runtime_options`（逐元素算子无归约，先不调 `pass_options`）；`x`/`y` 标注 `[pypto.DYNAMIC, ...], DT_BF16`；`k`/`b` 无标注；循环骨架与 L260-L265 同构；wrapper 五步套路与 L77-L100 同构。

2. 写 CPU golden 并自测（可在本机直接运行）：

   ```python
   def golden(x, k, b):
       return (x.float() * k + b).to(torch.bfloat16)

   x = torch.randn(64, 256, dtype=torch.bfloat16)
   ref = golden(x, 2.0, 1.0)
   # golden 自身即对照基准：与 NPU kernel 同为 FP32 计算、BF16 落盘
   ```

3. 有 NPU 环境时的完整验证（**待本地验证**）：

   ```bash
   cd pypto/src   # ST 的 sys.path 习惯是站在 ops-nn/quant/ai_infra_pypto_qat 的父级
   python -c "
   import torch, torch_npu
   torch.npu.set_device(0)
   from <你的目录>.ai_infra_scale_shift import ai_infra_scale_shift
   x = torch.randn(64, 256, dtype=torch.bfloat16, device='npu:0')
   y = ai_infra_scale_shift(x, k=2.0, b=1.0)
   ref = (x.float() * 2.0 + 1.0).to(torch.bfloat16)
   print(torch.equal(y.cpu(), ref.cpu()))   # 两侧都是 FP32 算完再转 BF16，预期 True
   "
   ```

**需要观察的现象**：第 3 步若环境就绪，应输出 `True`——因为 kernel 与 golden 的数值路径完全相同（BF16 输入 → FP32 中间 → BF16 输出），理论上逐位一致；若为 `False`，优先排查 tile 尾块是否覆盖了全部行（n=64 时 `unroll_list=[512,32,8]` 会退化成 32×2）。

**预期结果**：无 NPU 环境时，交付物是「kernel + wrapper + golden」三件套代码与一张自查清单；有 NPU 环境时追加逐位一致性的实测结论。环境前置：u1-l3 的 A2/A3 容器 + 容器内已安装 pypto 包（安装方式仓库未记载，待确认）+ `torch_npu`。

**风险提示**：若把此文件放进 `op_code/` 并被测试的 `sys.path` 扫到，可能影响官方 ST 收集——所以步骤 1 明确要求放在仓库目录之外。

## 6. 本讲小结

- pypto 是用 Python DSL 写 NPU 向量算子的路线：一个 `.py` 文件（kernel + wrapper）替代 ascendc 的四层结构，`import` 即部署，wrapper 直接收发 torch 张量；框架编译器本身不在本仓库。
- kernel 签名即张量契约：`pypto.Tensor([pypto.DYNAMIC, ...], pypto.DT_BF16)` 对标 `_def.cpp` 的原型声明，无标注参数是 Host 预计算的标量（对标 Attr）；输出采用目标传递风格，由 wrapper `torch.empty` 分配后按位置传入——**参数顺序是硬契约**。
- 数据流三原语 `view`（取块）/ `cast`（精度上抛 FP32 再落回 BF16）/ `assemble`（镜像写回）加上 torch 风格的计算原语，支撑了 LSQ+ 量化公式链；掩码用「放大 + clip」的数值技巧生成，全程无分支。
- 切分内嵌于 kernel：`loop_unroll`（unroll_list 大块优先、余量降级收尾）+ `set_vec_tile_shapes`（tile 形状跟随归约模式，可在 kernel 中途重设）；`runtime_options`/`pass_options` 是编译期旋钮，全仓库逐算子调优。
- 跨 tile 归约用「循环外 `pypto.full` 累加器 + 循环内切片赋值累加」的惯用法（per_tensor 反向的 grad_scale）；`view(-1, group_size)` 让用户接口形状与 kernel 接口形状解耦。

## 7. 下一步学习建议

本讲只解剖了**编程模型**，刻意绕开了梯度。下一讲 **u7-l2（QAT 对称量化算子：per_tensor 与 per_channel）**将进入算法层：STE 为什么能让不可导的 `round` 参与训练、对称量化的五步梯度公式如何对应到 `ai_infra_qat_symmetric_per_channel_backward_kernel` 的逐行代码（[L304-L376](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L304-L376)），以及 docs 约束（M∈[128,3072] 且被 128 整除）背后的硬件对齐原因。

继续阅读的建议路径：

1. [docs/qat_ops.md 的反向算子章节](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/docs/qat_ops.md#L132-L270)——先看对称 per_tensor 的五步梯度推导，数学准备量最小。
2. 对照本讲 4.3.3 的数值掩码技巧，预读 [L345-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L345-L352) 的注释——作者亲笔解释了「规避 where」的动机。
3. 有余力的读者可以横向对比 u2 单元：同一个「校验—切分—计算—写回」的骨架，ascendc 用四个文件、三种语言层表达，pypto 用 60 行 Python 表达——体会 DSL 的抽象边界在哪里（什么时候够用、什么时候必须下探到 Ascend C，比如需要 Cube 矩阵乘的 Attention 族）。
