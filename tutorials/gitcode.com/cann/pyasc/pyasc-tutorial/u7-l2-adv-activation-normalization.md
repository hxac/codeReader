# 高阶 API（二）：激活、归一化与融合算子开发

## 1. 本讲目标

学完本讲，你应该能够：

1. 分清 pyasc「基础向量 API」与「高阶 API」的界限，知道 `asc.adv.*` 命名空间下有哪些现成的高阶算子（swiglu、softmax、rmsnorm、tanh 等），以及它们分别定义在哪个源码文件。
2. 读懂三个真实示例的完整实现：06_gelu（用基础算子 + `asc.adv.tanh` 手工组合 GELU）、07_swiglu（直接调用 `asc.adv.swiglu` 融合 API）、08_rmsnorm（`asc.adv.rmsnorm` + `RmsNormTiling` 切分描述）。
3. 理解 TBuf 复用与 `asc.pipe_barrier(asc.PipeID.PIPE_V)` 在「同一 UB 缓冲上连续多步向量计算」中的作用。
4. 掌握融合算子的三种开发套路，并能独立实现一个简单融合激活算子（如 z = max(0, x+y)）。

本讲是第 7 单元第二讲，承接 u2-l5（基础 API 与 OverloadDispatcher）、u2-l6（TPipe/TQue/TBuf 框架）与 u7-l1（高阶 API 之 Matmul）已建立的认识。

## 2. 前置知识

**什么是激活函数与归一化。** 神经网络里，激活函数对每个元素做一次非线性变换（GELU、SwiGLU、ReLU、tanh），归一化则对一行的统计量（如均方根）做缩放（RMSNorm）。它们的特点是：计算量不大，但步骤多、访存密集，非常适合「一次搬进 UB，多步原地算完，再一次性搬出」的融合写法——这正是自定义算子的价值所在。

**高阶 API vs 基础 API。** u2-l5 讲过的 `asc.add`、`asc.mul` 等属于基础向量 API，一个调用对应一条 Ascend C 向量指令。高阶 API（`asc.adv.*`）则用一个 Python 函数封装**一整套多步计算**：例如 `asc.adv.rmsnorm` 内部完成平方、沿行归约、开方取倒、乘 gamma 等一长串动作，对应的 Ascend C 实现是头文件里的内联函数（编译器负责展开），而不是单条指令。高阶 API 常常还需要一个 **tiling 结构体**（如 `RmsNormTiling`）告诉它数据如何切分。

**TQue 与 TBuf 的分工（回顾 u2-l6）。** TQue 是带队列语义的缓冲：`alloc_tensor → enque → deque → free_tensor` 四步生命周期，队列自带内存互斥与隐式同步，用于 GM↔UB 的搬运边界。TBuf 是纯内存复用的缓冲（无队列语义），用 `get(dtype, len=...)` 从中切出 LocalTensor 视图，典型用途有二：多步计算之间的**临时中间值**，以及**一次加载、全程复用**的小块常量（如 RMSNorm 的 gamma 权重）。

**PIPE_V 屏障。** 向量算子下发到 Vector 流水线后是异步执行的。当多条向量指令读写**同一块 UB 缓冲**且后一条依赖前一条的结果时，必须插入 `asc.pipe_barrier(asc.PipeID.PIPE_V)` 保证同一流水线内按程序序完成。它与 u2-l4 讲的跨流水线 `set_flag/wait_flag` 不同：pipe_barrier 作用于单条流水线内部的排序，是最轻量的同步手段。

**RuntimeNumeric 与 `_mat`（回顾 u2-l3、u5-l6）。** 高阶 API 的标量参数（如 `epsilon`、`scalar_value`）类型标注为 `RuntimeNumeric`，意味着既可传 Python 立即数，也可传设备侧 IR 值；内部经 `materialize_ir_value`（惯用别名 `_mat`）统一物化成 IR 常量。这是读高阶 API 源码的固定套路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/06_gelu/gelu.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py) | 用基础算子 + `asc.adv.tanh` 手工组合 GELU（tanh 近似九步），演示 TBuf 复用与 PIPE_V 同步 |
| [examples/07_swiglu/swiglu.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py) | 直接调用 `asc.adv.swiglu` 融合 API，演示双路 VECIN 队列与两段拼接的 GM 布局 |
| [examples/08_rmsnorm/rmsnorm.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py) | `asc.adv.rmsnorm` + `RmsNormTiling`，演示 gamma 常驻 TBuf、标量级 get_value/set_value |
| [python/asc/language/adv/activation.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/activation.py) | 高阶激活 API：`softmax` 与 `swiglu` 的 Python 前端实现 |
| [python/asc/language/adv/math.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/math.py) | 高阶数学 API（注意：`asc.adv.tanh` 在这里，不在 activation.py），统一的 `math_op_impl` 模式 |
| [python/asc/language/adv/normalization.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/normalization.py) | 高阶归一化 API：`rmsnorm` 的 Python 前端实现 |
| [python/asc/language/adv/tiling.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py) | tiling 结构体定义：`RmsNormTiling`、`SoftmaxTiling`、`TCubeTiling` |
| [python/asc/language/basic/block_sync.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py) | `pipe_barrier`、`set_flag`/`wait_flag` 等同步原语的 Python 前端 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 本讲的对照样本：手动 LocalTensor + 手动三对同步的最简风格 |

一个容易踩的坑先说在前面：虽然本讲主题叫「activation.py 与 normalization.py」，但 **`asc.adv.tanh` 实际定义在 `adv/math.py`**。`adv/__init__.py` 把 activation、math、normalization、tiling 等子模块的名字统一汇入 `asc.adv` 命名空间（见 [python/asc/language/adv/__init__.py:L9-L60](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/__init__.py#L9-L60)），所以调用入口一致，源码位置却分散——检索高阶 API 实现时先查这个 `__init__.py` 的导入清单。

## 4. 核心概念与源码讲解

### 4.1 activation 高阶 API：swiglu 与 softmax

#### 4.1.1 概念说明

`adv/activation.py` 目前提供两个高阶激活接口：

- `softmax`：按最后一维做归一化指数，输出 dst 之外还产出 sum、max 两个辅助张量；
- `swiglu`：SwiGLU 融合激活，一条调用完成「Swish 激活 + 逐元素乘」两件事。

它们与 u2-l5 的 `asc.add` 这类基础 API 的本质区别在于：一个高阶 API 调用对应 Ascend C 侧一个**多步内联实现**（可能内部自带临时空间、自带同步），而不是单条向量指令。以 swiglu 为例，其计算公式（摘自源码 docstring）为：

\[
\text{Swish}(x) = \frac{x}{1 + e^{-\beta x}}, \qquad \text{SwiGLU} = \text{src}_0 \otimes \text{Swish}(\text{src}_1)
\]

若用基础 API 手工实现，需要 exp、除法/倒数、乘法至少三步；高阶 API 一步到位，且中间同步由实现内部处理。

#### 4.1.2 核心流程

以 `asc.adv.swiglu(dst, src0, src1, scalar_value=β, cal_count=n)` 为例，Python 前端只做三件事：

1. 把可选的 `shared_tmp_buffer` 从 LocalTensor 转成 IR 句柄（没传就是 None，表示让框架接口自行申请临时空间）；
2. 把标量 `scalar_value` 经 `_mat(scalar_value, dst_tensor.dtype)` 物化为与目的张量同 dtype 的 IR 常量；
3. 调用 `global_builder.get_ir_builder().create_asc_SwiGLUOp(...)` 生成一个 IR 操作。

之后的事都交给后端：IR 操作经 Pass 处理后，由发射层翻译成一条 `SwiGLU(...)` 的 Ascend C 调用（第 5、6 单元讲过的链路）。也就是说，高阶 API 的 Python 侧成本极低——它只是「参数规整 + 建一个 Op」。

#### 4.1.3 源码精读

swiglu 的完整实现只有 6 行：

```python
tmp_buffer_ir = shared_tmp_buffer.to_ir() if shared_tmp_buffer is not None else None
scalar_val_ir = _mat(scalar_value, dst_tensor.dtype).to_ir()
cal_count_ir = _mat(cal_count).to_ir() if cal_count is not None else None
global_builder.get_ir_builder().create_asc_SwiGLUOp(dst=dst_tensor.to_ir(), srcTensor0=src_tensor0.to_ir(),
                                                    srcTensor1=src_tensor1.to_ir(), scalarValue=scalar_val_ir,
                                                    sharedTmpBuffer=tmp_buffer_ir, calCount=cal_count_ir)
```

这段代码位于 [python/asc/language/adv/activation.py:L236-L241](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/activation.py#L236-L241)：三个可选参数（临时缓冲、标量、cal_count）各自判空后物化为 IR，最后一次性建 `SwiGLUOp`。函数签名与参数约束在 [python/asc/language/adv/activation.py:L152-L154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/activation.py#L152-L154)，docstring 中的公式与 Ascend C 原型对照在 [python/asc/language/adv/activation.py:L156-L202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/activation.py#L156-L202)——docstring 列出了四种 C++ 重载（有无 sharedTmpBuffer × 有无 calCount），Python 侧用两个 `Optional` 参数统一表达。

同文件的 softmax 采用完全相同的结构，见 [python/asc/language/adv/activation.py:L144-L149](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/activation.py#L144-L149)：`temp_buffer` 转 IR、三个模板开关（reuse_source/basic_block/data_format_nz）作为命名参数直接传入 `create_asc_SoftMaxOp`。这两个函数展示了「布尔模板参数 → Python 关键字参数」的标准映射——回忆 u5-l3：可推导的类型模板参数（`typename T`）不进 IR，由 tensor 类型反推；布尔开关（isBasicBlock 等）进 IR 成为属性，发射时拼回 C++ 模板实参。

再看 `asc.adv.tanh` 的真实定义处（adv/math.py）：

```python
@require_jit
@set_math_docstring(api_name="Tanh", append_text="按元素做逻辑回归Tanh。")
def tanh(dst: LocalTensor, src: LocalTensor, count: Optional[RuntimeInt] = None,
         temp_buffer: Optional[LocalTensor] = None, is_reuse_source: RuntimeBool = False) -> None:
    math_op_impl((dst, src), count, temp_buffer, is_reuse_source, "create_asc_TanhOp")
```

见 [python/asc/language/adv/math.py:L372-L376](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/math.py#L372-L376)。math.py 中 acos、exp、log、sin 等几十个函数全部是这一行的变体，公共逻辑沉淀在 `math_op_impl`（[python/asc/language/adv/math.py:L19-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/math.py#L19-L29)）：`check_type` 校验 count、temp_buffer 判空转 IR、`is_reuse_source` 物化为 bit 类型，最后用 `getattr(builder, build_method)` 按函数名动态调用 `create_asc_TanhOp` / `create_asc_ExpOp` / …。**builder 方法名以字符串传入**是这套「量产」模式的关键——新增一个数学高阶算子只需三行。

注意 tanh 与 swiglu 的一个差别：tanh 是**逐元素**数学函数，语义上与基础一元算子同级，只是实现上走高精度查表内联；swiglu 则是真正的「融合」函数。两者在 Python 前端却共用同一套三段式（overload 存根 + require_jit + 建 Op），再次印证 u5-l6 总结的读码套路。

#### 4.1.4 代码实践

**实践目标**：验证 swiglu 高阶 API 的参数映射与参考公式一致。

**操作步骤**：

1. 打开 [examples/07_swiglu/swiglu.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py)，找到 kernel 内的调用行 `asc.adv.swiglu(y_local, up_local, gate_local, cal_count=tile_length)`（[examples/07_swiglu/swiglu.py:L118](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py#L118)）。
2. 对照 Host 侧参考实现 `swiglu_reference`（[examples/07_swiglu/swiglu.py:L144-L145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py#L144-L145)）：`gate * torch.sigmoid(gate) * up`。
3. 按 `swiglu(dst, src_tensor0, src_tensor1)` 的形参顺序，把调用改写为 `asc.adv.swiglu(y_local, gate_local, up_local, cal_count=tile_length)`（交换第 2、3 个实参）。
4. 运行 `python3 examples/07_swiglu/swiglu.py -r Model`。

**需要观察的现象**：改写前断言通过；改写后由于 `src1`（Swish 的作用对象）从 gate 变成了 up，输出变为 `up * sigmoid(up) * gate`，与参考实现不再一致，`torch.allclose` 断言失败（或误差明显超出 atol=1e-3）。

**预期结果**：确认 `src_tensor1` 才是 Swish 激活的作用对象、`src_tensor0` 是逐元素乘的另一方。若在无 NPU 环境下无法运行 Model 仿真，此实践标注为「待本地验证」，可改为纯源码推演：手推两组 2×2 的小矩阵在两种实参顺序下的结果差异。

#### 4.1.5 小练习与答案

**练习 1**：`asc.adv.tanh` 定义在哪个文件？为什么它不在 activation.py 里也能以 `asc.adv.tanh` 的名字被调用？

**答案**：定义在 `python/asc/language/adv/math.py`（L372-L376）。因为 `adv/__init__.py` 在 L11-L39 从 `.math` 导入了 `tanh` 并列入 `__all__`，Python 的包机制把它重新导出到 `asc.adv` 命名空间；调用入口与定义文件分离是 pyasc 高阶 API 的常态。

**练习 2**：swiglu 的 `scalar_value` 参数默认值是多少？它在 Python 前端经历了什么处理？

**答案**：默认 `1.0`（即 β=1，Swish 退化为 Silu：x·sigmoid(x)）。前端在 activation.py L237 用 `_mat(scalar_value, dst_tensor.dtype)` 把它物化为与目的张量同 dtype 的 IR 常量，再作为 `scalarValue` 操作数传入 `create_asc_SwiGLUOp`。

**练习 3**：高阶 API 与基础向量 API 在「一个调用对应什么」上有何不同？

**答案**：基础 API（如 `asc.add`）对应一条 Ascend C 向量指令；高阶 API 对应一个多步内联实现（头文件内联函数），内部可能包含多条指令、临时空间与内部同步，Python 侧仅生成一个 IR 操作，展开发生在 Ascend C 编译阶段。

### 4.2 normalization 高阶 API：rmsnorm 与 RmsNormTiling

#### 4.2.1 概念说明

RMSNorm（Root Mean Square Layer Normalization）是 Transformer 常用的归一化，对输入的最后一维（hidden 维）做：

\[
\text{RmsNorm}(x) = \frac{x}{\sqrt{\frac{1}{H}\sum_{i=1}^{H} x_i^2 + \epsilon}} \cdot \gamma
\]

其中 H 是 hidden 维长度，γ 是可学习权重，ε 是防除零小量。与逐元素的激活不同，它沿行做**归约**（求平方和），因此实现上必须知道数据如何分行分块——这就是 `RmsNormTiling` 存在的原因：它是传给 Ascend C 实现的「切分说明书」，告诉 API 每块多少行、多少列、对齐到多少、有没有尾部。

`RmsNormTiling` 是一个 Struct（u3-l3 讲过 Struct 的「三面体」：Host 侧 ctypes 打包、IR 侧 PyStructType、设备侧本地副本），在 kernel 内部用关键字参数构造，字段与 Ascend C 的 `RmsNormTiling` 结构体一一对应。

#### 4.2.2 核心流程

`asc.adv.rmsnorm(dst, src, gamma, epsilon, tiling, temp_buffer=None, basic_block=False)` 的完整流程：

1. `temp_buffer` 判空转 IR（与 swiglu 相同的可选临时空间模式）；
2. `epsilon = _mat(epsilon, src.dtype)`：注意这里物化时用的是 **src 的 dtype**，保证精度类型与输入一致；
3. 调 `create_asc_RmsNormOp(...)`，把 basicBlock 开关、四个张量、epsilon、tiling 一并传入。

tiling 的构造发生在**用户 kernel 代码**里（不是 API 内部）：08_rmsnorm 示例写了一个 `rmsnorm_make_tiling` 子函数，按「本次处理多少行、hidden 多长、主块多长、对齐到多少」填好 12 个字段后返回。设备侧代码按分块循环逐块构造 tiling 并调用 rmsnorm。

#### 4.2.3 源码精读

rmsnorm 的 Python 前端是全 adv 包里最短的一个：

```python
@overload
def rmsnorm(dst, src, gamma, epsilon: Union[float, int], tiling: RmsNormTiling,
            temp_buffer: Optional[LocalTensor] = None, basic_block: bool = False) -> None: ...

@require_jit
def rmsnorm(dst, src, gamma, epsilon: RuntimeNumeric, tiling: RmsNormTiling,
            temp_buffer: Optional[LocalTensor] = None, basic_block: bool = False) -> None:
    temp_buffer = temp_buffer.to_ir() if temp_buffer is not None else None
    epsilon = _mat(epsilon, src.dtype)
    global_builder.get_ir_builder().create_asc_RmsNormOp(basicBlock=basic_block, dst=dst.to_ir(),
                                                         src=src.to_ir(), gamma=gamma.to_ir(),
                                                         epsilon=epsilon.to_ir(), tiling=tiling.to_ir(),
                                                         sharedTmpBuffer=temp_buffer)
```

见 [python/asc/language/adv/normalization.py:L18-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/normalization.py#L18-L31)。注意 overload 存根用 `Union[float, int]` 面向类型检查器，真实实现用 `RuntimeNumeric` 兼容设备侧标量——这是 adv 包的通用写法。

`RmsNormTiling` 的 12 个字段定义在 [python/asc/language/adv/tiling.py:L69-L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py#L69-L81)，每个字段用 `Field(dtype=..., default=..., name=...)` 声明，`name` 是 Ascend C 结构体里的成员名（如 `bLength`、`mainBshLength`）。关键字段含义：

| 字段 | 含义 |
| --- | --- |
| `b_length` / `s_length` | 批大小 / 本次处理的行数 |
| `h_length` / `original_h_length` | hidden 维长度（对齐后 / 原始） |
| `reciprocal_of_h_length` | 1/H，避免设备上做除法 |
| `main_bsh_length` | 主块元素总数（b×s×h） |
| `main_bs_length` / `main_bs_length_align` | 行数 / 对齐后的行数（rmsnorm 要求按 16 行对齐） |
| `loop_round` | 主循环圈数 |
| `input_tail_pos` / `tail_*` | 尾块位置与尾部长度 |

示例中的构造代码 [examples/08_rmsnorm/rmsnorm.py:L137-L143](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L137-L143)：`s_length=row_count`（本块行数）、`h_length=hidden_size`、`reciprocal_of_h_length=1.0/float(hidden_size)`（Host 侧编译期算好）、`main_bsh_length=bsh_length`、`loop_round=1`、尾块长度 0（示例保证整块处理）。而**对齐**在调用方完成：kernel 里 `aligned_rows = ((max_rows + 15) // 16) * 16`（[examples/08_rmsnorm/rmsnorm.py:L86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L86)）。

kernel 主体的调用点在 [examples/08_rmsnorm/rmsnorm.py:L113-L126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L113-L126) 的 `rmsnorm_process_block` 子函数：alloc x_local → data_copy 搬入 → enque/deque → alloc y_local → `asc.adv.rmsnorm(y_local, x_local, gamma_local, eps, tiling, basic_block=True)`（L121）→ free/enque/deque → data_copy 搬出。这正是 u2-l6 的标准 TQue 四步范式，`basic_block=True` 表示 shape 与 tiling 满足基本块要求、可走高性能路径。

#### 4.2.4 代码实践

**实践目标**：弄清 RmsNormTiling 各字段的取值来源，理解「Host 算好、Device 只读」的切分信息流。

**操作步骤**：

1. 阅读 kernel 中的分组逻辑 [examples/08_rmsnorm/rmsnorm.py:L94-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L94-L105)：`full_groups = rows_per_core // max_rows`、`rem_rows = rows_per_core % max_rows`，整块循环调 `rmsnorm_process_block`，剩余行单独处理一次。
2. 对照 Host 侧 `compute_rmsnorm_launch_params`（[examples/08_rmsnorm/rmsnorm.py:L44-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L44-L51)）与 `_rows_per_call`（[examples/08_rmsnorm/rmsnorm.py:L28-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L28-L33)）。
3. 把输入 shape 从 `(2, 8, 256)` 改为 `(2, 8, 1024)`，推演 `hidden_size`、`max_rows`、`rows_per_core`、`full_groups`、`rem_rows` 各是多少，再运行 `python3 examples/08_rmsnorm/rmsnorm.py -r Model` 验证。

**需要观察的现象**：hidden_size 变大后 `_rows_per_call` 从 8 降为 4（单次调用的行数减少以控制 UB 占用），tiling 的 `s_length` 随之变化，而 `h_length` 恒等于 gamma 的长度；结果仍与 `rmsnorm_reference`（[examples/08_rmsnorm/rmsnorm.py:L191-L193](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L191-L193)）一致。

**预期结果**：tiling 的行数字段由 launch 参数（ConstExpr）决定，hidden 字段由 gamma 形状决定，两者都是编译期常量——改 hidden_size 会生成新的缓存 key（回忆 u3-l8：ConstExpr 值进缓存 key）。若无法本地运行，此步骤标注「待本地验证」，推演结果仍可完成。

#### 4.2.5 小练习与答案

**练习 1**：`epsilon` 为什么用 `src.dtype` 物化而不用固定的 float32？

**答案**：half 输入时若 epsilon 是 float32 常量，会在 Ascend C 侧引入类型不匹配或额外转换；与 src 同 dtype 保证常量直接参与同类型运算（normalization.py L28）。

**练习 2**：为什么 tiling 里存 `reciprocal_of_h_length = 1/H` 而不是让设备上现算？

**答案**：H 是编译期已知的 ConstExpr，倒数可以在构造 tiling 时（Python 表达式 `1.0 / float(hidden_size)`，rmsnorm.py L141）一次算好；设备上除法远慢于乘法，存倒数把设备侧的除法换成乘法。

**练习 3**：`basic_block=True` 是运行时参数还是模板参数？它如何影响生成的 Ascend C 代码？

**答案**：它是编译期布尔开关，作为 `basicBlock` 属性进入 `RmsNormOp`（normalization.py L29），发射层拼回 C++ 模板实参 `isBasicBlock=true`，使能基本块高性能路径——这是 u5-l3「布尔模板参数殿后为属性」规则的直接体现。

### 4.3 TBuf 复用与 PIPE_V 同步：gelu 九步流水

#### 4.3.1 概念说明

GELU 没有 `asc.adv.gelu` 这样的一步式 API，06 示例用 **tanh 近似公式**手工组合：

\[
\text{GELU}(x) \approx 0.5\,x\left(1 + \tanh\left(\sqrt{2/\pi}\,\left(x + 0.044715\,x^3\right)\right)\right)
\]

按算子粒度拆开是九步：`x·x`、`·x`、`·0.044715`、`+x`、`·√(2/π)`、`tanh`、`+1.0`、`·0.5`、`x·tmp`。前八步的中间结果全部落在**同一个** TBuf 切出的 `tmp` 张量上——这就是 TBuf 复用：一块 UB 内存，反复当草稿纸。而正因为每一步都读写同一块缓冲，相邻两条向量指令之间必须插 `pipe_barrier(PIPE_V)` 排序。

#### 4.3.2 核心流程

gelu_kernel（AIV_ONLY，纯向量算子）的结构：

```
初始化：TPipe + VECIN 队列 + VECOUT 队列（各 BUFFER_NUM=2 块）+ VECCALC 的 TBuf tmp
循环 tile 次：
    copy_in:  alloc → data_copy(GM→UB) → enque
    compute:  deque x → alloc y → tmp = TBuf.get()
              九步计算，步间 pipe_barrier(PIPE_V)
              enque y → free x
    copy_out: deque y → data_copy(UB→GM) → free y
```

同步分两层：**队列边界**（MTE2↔V↔MTE3）由 TQue 的 enque/deque 隐式承担（u2-l6）；**计算内部**（同一 tmp 上的串行依赖链）由 PIPE_V 屏障显式承担。最后一步 `mul(y_local, x_local, tmp)` 之后不需要屏障——enque 本身就有把「计算完成」通知搬运流水线的语义。

#### 4.3.3 源码精读

缓冲初始化（[examples/06_gelu/gelu.py:L68-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L68-L75)）：

```python
pipe = asc.TPipe()
in_queue = asc.TQue(asc.TPosition.VECIN, 1)
out_queue = asc.TQue(asc.TPosition.VECOUT, 1)
tmp_buf = asc.TBuf(asc.TPosition.VECCALC)

pipe.init_buffer(que=in_queue, num=BUFFER_NUM, len=tile_length * x.dtype.sizeof())
pipe.init_buffer(que=out_queue, num=BUFFER_NUM, len=tile_length * y.dtype.sizeof())
pipe.init_buffer(buf=tmp_buf, len=tile_length * x.dtype.sizeof())
```

三个缓冲三种角色：in_queue/out_queue 是搬运队列（双缓冲乒乓），tmp_buf 是计算草稿（单份即可——它只在 compute 内部使用，不跨 tile 传递状态）。

九步计算主体（[examples/06_gelu/gelu.py:L94-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L94-L117)）：

```python
x_local = in_queue.deque(y_gm.dtype)
y_local = out_queue.alloc_tensor(y_gm.dtype)
tmp = tmp_buf.get(y_gm.dtype)          # TBuf 切出草稿张量

asc.mul(tmp, x_local, x_local, count=tile_length)      # 1. x²
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.mul(tmp, tmp, x_local, count=tile_length)          # 2. x³
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.muls(tmp, tmp, GELU_CUBIC_COEFF, count=tile_length)  # 3. ·0.044715
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.add(tmp, tmp, x_local, count=tile_length)          # 4. +x
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.muls(tmp, tmp, GELU_TANH_SCALE, count=tile_length)   # 5. ·√(2/π)
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.adv.tanh(tmp, tmp, count=tile_length)              # 6. tanh（高阶 API）
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.adds(tmp, tmp, 1.0, count=tile_length)             # 7. +1
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.muls(tmp, tmp, 0.5, count=tile_length)             # 8. ·0.5
asc.pipe_barrier(asc.PipeID.PIPE_V)
asc.mul(y_local, x_local, tmp, count=tile_length)      # 9. x·tmp

out_queue.enque(y_local)
in_queue.free_tensor(x_local)
```

几个要点：

- **基础与高阶混排**：第 1-5、7-9 步是 u2-l5 的基础算子（mul/muls/add/adds），第 6 步是高阶 `asc.adv.tanh`——两者在同一依赖链上无缝衔接，对使用者来说只是函数名不同。
- `tmp = tmp_buf.get(y_gm.dtype)` 调用的是 TBuf.get，实现见 [python/asc/language/fwk/tpipe.py:L241-L251](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L241-L251)：每次 get 生成一个 `TBufGetTensorOp`，按 dtype+len 切出一个 LocalTensor 视图。gelu 里每个 tile 都重新 get 一次同一块缓冲。
- `pipe_barrier` 的 Python 前端只有一行（[python/asc/language/basic/block_sync.py:L24-L27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py#L24-L27)）：`create_asc_PipeBarrierOp(pipe)`。`PipeID.PIPE_V` 的枚举值定义在 [python/asc/language/core/enums.py:L172-L180](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L172-L180)（与 PIPE_MTE2/PIPE_MTE3 等并列）。
- 常量 `GELU_CUBIC_COEFF=0.044715`、`GELU_TANH_SCALE=√(2/π)` 定义在模块顶部 [examples/06_gelu/gelu.py:L31-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L31-L32)，与参考实现 [examples/06_gelu/gelu.py:L148-L149](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L148-L149) 共用，保证两侧公式严格一致。

对照 08_rmsnorm 中 TBuf 的另一种用法——**常驻复用**：gamma 权重一次搬入 TBuf，全程只读：

```python
gamma_local = gamma_buf.get(gamma_gm.dtype, len=hidden_size)
rmsnorm_load_gamma(gamma_gm, gamma_local, hidden_size)
```

见 [examples/08_rmsnorm/rmsnorm.py:L91-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L91-L92)，加载子函数内用一对 `set_flag/wait_flag(MTE2_V)` 手动等搬运完成（[examples/08_rmsnorm/rmsnorm.py:L146-L150](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L146-L150)）——因为这次搬运发生在 TQue 队列纪律之外（直接 TBuf.get + data_copy），跨流水线同步要自己补。这是「TBuf 免队列但免不了同步」的最佳示例。

#### 4.3.4 代码实践

**实践目标**：验证 PIPE_V 屏障在串行依赖链上的必要性。

**操作步骤**：

1. 运行原版：`python3 examples/06_gelu/gelu.py -r Model`，确认通过。
2. 删除第 2 步 `asc.mul(tmp, tmp, x_local, ...)` 前面的那个 `asc.pipe_barrier(asc.PipeID.PIPE_V)`（[examples/06_gelu/gelu.py:L99](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L99)），再次运行。
3. 恢复后，再试删掉第 6 步 tanh 后面的屏障（L109）对比。

**需要观察的现象**：Model 仿真器可能仍然给出正确结果（仿真器对流水线的模拟可能偏宽松），但在真机上、或数据量/时序变化时，相邻两条向量指令对同一 tmp 的读写可能乱序，输出出现随机错误。若 Model 模式下结果不变，这正是「仿真通过 ≠ 真机正确」的典型例子。

**预期结果**：理解屏障保护的是「同一缓冲上的生产者-消费者顺序」；删屏障不改变程序语义描述，但破坏硬件执行顺序保证。此实践在真机上的表现标注「待本地验证」（需要 NPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：gelu 的 tmp_buf 为什么只分配 `tile_length * dtype.sizeof()` 一份，而不像队列那样给 BUFFER_NUM=2 份？

**答案**：tmp 只在 compute 子函数内部作草稿，用完即弃、不跨 tile 传递状态，compute 内部是严格串行的九步，不存在双缓冲重叠的需求；而 in/out 队列要支撑「上一 tile 搬出与下一 tile 搬入/计算」的重叠，需要乒乓两块。

**练习 2**：gelu 九步里哪一步是高阶 API？它的 temp_buffer 参数为什么没传？

**答案**：第 6 步 `asc.adv.tanh(tmp, tmp, count=tile_length)`。不传 temp_buffer 时框架接口自行申请临时空间（对应无 sharedTmpBuffer 的 Ascend C 重载，见 activation.py docstring 中 softmax 的四组原型同理）；只有 UB 紧张需要精确控制时才手工传入。

**练习 3**：08_rmsnorm 加载 gamma 时为什么用 `set_flag/wait_flag(MTE2_V)` 而 gelu 计算内部用 `pipe_barrier(PIPE_V)`？

**答案**：gamma 加载是**跨流水线**依赖（MTE2 搬运 → V 使用），必须用 HardEvent 事件对（u2-l4）；gelu 九步是**同一流水线内**（V→V）对同一缓冲的串行依赖，用 pipe_barrier 即可，开销更小。

### 4.4 融合算子套路：三种模式与示例对照

#### 4.4.1 概念说明

把前面三个示例抽象成三种可复用的开发模式：

| 模式 | 代表示例 | 适用场景 | 用户承担的工作 |
| --- | --- | --- | --- |
| A. 手工组合：基础算子 + 单个高阶数学函数 | 06_gelu | 目标函数无现成融合 API，可拆成逐元素表达式 | 自己拆步骤、自己管 TBuf 草稿与 PIPE_V 同步 |
| B. 一步融合：直接用高阶融合 API | 07_swiglu | 目标函数已有 `asc.adv.swiglu` 这类封装 | 只管 GM 布局与 TQue 队列纪律，中间同步 API 内部处理 |
| C. 归约类：高阶 API + tiling 切分描述 | 08_rmsnorm | 涉及沿行归约/查表等复杂内部结构 | 构造 tiling 结构体、处理分块与对齐、常量权重常驻 TBuf |

选型原则：先查 `asc.adv` 命名空间（`adv/__init__.py` 的 `__all__` 清单）有没有现成 API；有则走 B/C，没有且能拆成逐元素式则走 A；涉及矩阵乘则回到 u7-l1 的 Matmul。

#### 4.4.2 核心流程

无论哪种模式，Host 侧的骨架完全一致（这是融合算子的「外壳套路」）：

1. **推 launch 参数**：按总长度、dtype 宽度、DMA 友好块大小算出 `effective_cores / block_length / tile_length`，block_length 上调为 tile 整数倍；
2. **补齐**：`padded_len = effective_cores * block_length` 超过实际长度时 Host 侧补零；
3. **下发**：`kernel[effective_cores, rt.current_stream()](...)`；
4. **校验**：与 torch 参考实现 `torch.allclose` 对比。

Device 侧差异只在 compute 段：模式 A 是九步串行，模式 B 是一行 `asc.adv.swiglu(...)`，模式 C 是构造 tiling + 一行 `asc.adv.rmsnorm(...)`。

#### 4.4.3 源码精读

**模式 B 的双路 VECIN**。07 示例的 kernel 建了**两个独立的 VECIN 队列**分别搬运 gate 与 up 两路输入（[examples/07_swiglu/swiglu.py:L96-L103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py#L96-L103)）：

```python
pipe = asc.TPipe()
in_queue_gate = asc.TQue(asc.TPosition.VECIN, 1)
in_queue_up = asc.TQue(asc.TPosition.VECIN, 1)
out_queue = asc.TQue(asc.TPosition.VECOUT, 1)
```

循环体（[examples/07_swiglu/swiglu.py:L105-L126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py#L105-L126)）：两路各自 alloc→data_copy→enque，再各自 deque，一次 `asc.adv.swiglu(y_local, up_local, gate_local, cal_count=tile_length)` 融合计算，最后统一搬出。GM 布局由 Host 侧 `torch.cat([gate_pad, up_pad])` 拼成前后两段（[examples/07_swiglu/swiglu.py:L134-L137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py#L134-L137)），kernel 里 up 路用 `half_len + i * tile_length` 偏移访问（L110）——**GM 上的一维拼接 + kernel 内偏移**是双输入算子最常见的布局手法。

**模式 A 的 Host 侧推参**。06 示例的 `compute_launch_params`（[examples/06_gelu/gelu.py:L39-L55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L39-L55)）：先按 `DATABLOCK_BYTES // dtype_size` 保证 32 字节对齐的向量粒度，再取 DMA 友好的 tile（512B 优先、数据不足退化 256B），从候选核数 `(1, 2, 4, 8)` 里挑够用的最小档，最后把 block_length 上调为 tile 的整数倍。`gelu_launch` 的补齐与切片返回在 [examples/06_gelu/gelu.py:L127-L145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L127-L145)。

**模式 C 的标量级读写**。08 示例在 kernel 尾部附加了一段逐行 rms 输出的计算（[examples/08_rmsnorm/rmsnorm.py:L158-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L158-L171)）：从 rms_buf 反复 `get(dtype, len=1)` 切出单元素视图，`asc.data_copy` 搬入单个值，`get_value(0)` 读成标量做除法，再 `set_value(row, ...)` 写回。`get_value/set_value` 是 LocalTensor 的标量访问接口（定义见 [python/asc/language/core/tensor.py:L356-L371](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L356-L371) 与 [python/asc/language/core/tensor.py:L437-L451](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L437-L451)），适合小规模、逐元素的Host 风格计算——但注意同一 TBuf 切出的多个视图在缓冲上是**重叠**的（都从头开始），这里靠「搬入→立即读→再写」的程序序串行复用，属于节省 UB 的技巧性写法，使用时要小心指令间的时序。

**与 01_add 的对照**。把 06/07/08 与 u1-l4 精读过的 01_add 放在一起看同步的演进：01_add 手工排布三个 LocalTensor 并显式写三对 `set_flag/wait_flag`（[examples/01_add/add.py:L45-L47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L45-L47)、[examples/01_add/add.py:L57-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L57-L69)）；06/07/08 把跨流水线同步交给 TQue 的 enque/deque，只在队列纪律覆盖不到的地方（计算内部串行、TBuf 直载）手工补屏障。功能越复杂，框架化风格的优势越明显。

#### 4.4.4 代码实践

**实践目标**：统计三种模式在「设备侧代码行数 vs 用户手写同步条数」上的差异，建立选型直觉。

**操作步骤**：

1. 分别数一下三个示例 kernel 部分（`@asc.jit` 函数体）的行数：06 约 40 行（不含 Host 侧）、07 约 40 行、08 约 60 行。
2. 数手工同步原语出现次数：06 中 `pipe_barrier` 8 次；07 中 0 次（全靠队列）；08 中 `set_flag/wait_flag` 各 1 次（gamma 加载）。
3. 把结果填进一张三行表格（模式 / 设备侧行数 / 手工同步数 / 高阶 API 调用数）。

**需要观察的现象**：模式 B 的同步数为 0——融合 API 把中间同步全部内化；模式 A 的同步数与计算步数成正比；模式 C 介于两者之间。

**预期结果**：得出结论「高阶 API 每多封装一步，用户就少写一对同步」，这正是 pyasc 把复杂算子做成 adv 层的动机。本实践为纯源码阅读型，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：如果要用 pyasc 实现 SiLU（x·sigmoid(x)，无门控分支），你会选哪种模式？

**答案**：模式 A。查 `adv/__init__.py` 的 `__all__`（以及 basic 层接口清单）都没有 silu 或 sigmoid，但 SiLU 可拆为 \( \sigma(x) = 1/(1+e^{-x}) \) 加逐元素乘：`tmp = exp(-x)` → `tmp = 1/(1+tmp)` → `y = x·tmp`。exp 有两个入口：基础层 `asc.exp`（[python/asc/language/basic/vec_unary.py:L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_unary.py#L92)）与高阶层 `asc.adv.exp`（math.py，带 `taylor_expand_level` 参数），倒数可再除一次或用 `asc.adv.tanh` 同层的接口思路；步间加 PIPE_V 屏障，整体仿照 06_gelu 的骨架。

**练习 2**：07 示例为什么把 gate 与 up 拼成一个大张量传入，而不是两个独立的 GlobalAddress 参数？

**答案**：两种做法都可行。拼接成一个张量只需一次 pad/offset 管理，kernel 内用一个 `fused + offset` 基址加 `half_len` 偏移即可访问两路；分成两个参数则每个 GlobalTensor 各自 set_global_buffer。示例选拼接布局是为了让「双路 VECIN + 单次 launch」的演示更紧凑（swiglu.py L134-L138）。

**练习 3**：三个示例都标注 `kernel_type=config.KernelType.AIV_ONLY`（如 [examples/06_gelu/gelu.py:L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/06_gelu/gelu.py#L58)），为什么？

**答案**：激活与归一化都是纯向量计算，不涉及矩阵乘（Cube），显式声明 AIV_ONLY 让编译目标直接定为 Vector 核（回忆 u3-l5：单目标 `dav-c220-vec`，无需 MIX 双目标编译与链接），编译更快、产物更小。Matmul 类算子才会推导为 MIX/AIC（u7-l1）。

## 5. 综合实践

实现一个 **ReLU 融合到 Add** 的算子：\( z = \max(0,\, x + y) \)，并对照 01_add 说明你在同步与缓冲管理上的选择。这是本讲四个最小模块的综合运用：模式 A 手工组合（add → maxs/relu）+ TBuf/TQue 分工 + PIPE_V 同步 + Host 侧套路。

**第一步：创建文件** `relu_add.py`（放examples 之外的自定义脚本即可），骨架仿照 06_gelu：

```python
# 示例代码：根据 examples/06_gelu/gelu.py 与 examples/01_add/add.py 改写
import torch
import asc
import asc.runtime.config as config
import asc.lib.runtime as rt

USE_CORE_NUM = 8
BUFFER_NUM = 2

@asc.jit(kernel_type=config.KernelType.AIV_ONLY)
def relu_add_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress,
                    block_length: asc.ConstExpr[int], tile_length: asc.ConstExpr[int]):
    offset = asc.get_block_idx() * block_length
    x_gm, y_gm, z_gm = asc.GlobalTensor(), asc.GlobalTensor(), asc.GlobalTensor()
    x_gm.set_global_buffer(x + offset)
    y_gm.set_global_buffer(y + offset)
    z_gm.set_global_buffer(z + offset)

    pipe = asc.TPipe()
    in_queue_x = asc.TQue(asc.TPosition.VECIN, 1)
    in_queue_y = asc.TQue(asc.TPosition.VECIN, 1)
    out_queue = asc.TQue(asc.TPosition.VECOUT, 1)
    tmp_buf = asc.TBuf(asc.TPosition.VECCALC)
    pipe.init_buffer(que=in_queue_x, num=BUFFER_NUM, len=tile_length * x.dtype.sizeof())
    pipe.init_buffer(que=in_queue_y, num=BUFFER_NUM, len=tile_length * x.dtype.sizeof())
    pipe.init_buffer(que=out_queue, num=BUFFER_NUM, len=tile_length * x.dtype.sizeof())
    pipe.init_buffer(buf=tmp_buf, len=tile_length * x.dtype.sizeof())

    total_tiles = block_length // tile_length
    for i in asc.range(total_tiles):
        x_local = in_queue_x.alloc_tensor(x_gm.dtype)
        y_local = in_queue_y.alloc_tensor(y_gm.dtype)
        asc.data_copy(x_local, x_gm[i * tile_length:], count=tile_length)
        asc.data_copy(y_local, y_gm[i * tile_length:], count=tile_length)
        in_queue_x.enque(x_local)
        in_queue_y.enque(y_local)

        x_local = in_queue_x.deque(x_gm.dtype)
        y_local = in_queue_y.deque(y_gm.dtype)
        z_local = out_queue.alloc_tensor(z_gm.dtype)
        t = tmp_buf.get(x_gm.dtype)

        asc.add(t, x_local, y_local, count=tile_length)      # 融合第一步：加法
        asc.pipe_barrier(asc.PipeID.PIPE_V)                  # t 上生产者→消费者排序
        asc.maxs(z_local, t, 0.0, count=tile_length)         # 融合第二步：max(·, 0)

        in_queue_x.free_tensor(x_local)
        in_queue_y.free_tensor(y_local)
        out_queue.enque(z_local)
        z_local = out_queue.deque(z_gm.dtype)
        asc.data_copy(z_gm[i * tile_length:], z_local, count=tile_length)
        out_queue.free_tensor(z_local)
```

说明两个候选写法：`asc.maxs(dst, src, scalar, count)`（[python/asc/language/basic/vec_binary_scalar.py:L86-L91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary_scalar.py#L86-L91)，逐元素与标量取大）或 `asc.relu(dst, src, count)`（[python/asc/language/basic/vec_unary.py:L190-L195](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_unary.py#L190-L195)，两者数学上对 float 等价）。

**第二步：Host 侧 launch 与校验**（可直接借用 01_add 的简化版，注意保证长度能被核数与 tile 整除）：

```python
# 示例代码
def relu_add_launch(x, y):
    z = torch.zeros_like(x)
    total = x.numel()
    block_length = total // USE_CORE_NUM
    tile_length = block_length // 2          # TILE_NUM=2
    relu_add_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length)
    return z

if __name__ == "__main__":
    config.set_platform(config.Backend.Model, None)
    size = 8 * 2048
    x = torch.rand(size, dtype=torch.float32) * 2 - 1
    y = torch.rand(size, dtype=torch.float32) * 2 - 1
    z = relu_add_launch(x, y)
    assert torch.allclose(z, torch.relu(x + y)), "mismatch!"
    print("relu_add passed")
```

**第三步：运行与观察**：

1. `python3 relu_add.py`（Model 模式，无需 NPU），预期输出 `relu_add passed`。运行结果待本地验证。
2. 设置 `PYASC_DUMP_PATH=/tmp/pyasc_dump` 重跑，打开导出的 `ascendc.cpp`，找到 `Add`、`Maxs`（或 `Relu`）两条 Ascend C 调用，确认融合的两步都出现在同一 kernel 内、中间没有 GM 往返。
3. 对照 01_add 写一段分析，回答：①跨流水线同步你交给了谁（TQue 的 enque/deque，而非手写三对 set_flag/wait_flag）；②计算内部同步为何只需一条 PIPE_V（只有一处生产者→消费者依赖：add 写 t、maxs 读 t）；③tmp 为何用 TBuf 单份而非队列双份（草稿不跨 tile、串行使用）。

**验收标准**：数值与 `torch.relu(x + y)` 一致（atol 1e-6 量级）；dump 的 ascendc.cpp 中能看到融合的两条调用；能说清三问的答案。

## 6. 本讲小结

- `asc.adv` 命名空间按源码分布在 activation.py（softmax/swiglu）、math.py（tanh/exp/sin 等逐元素数学函数，**tanh 不在 activation.py**）、normalization.py（rmsnorm）、matmul.py 等文件，统一经 `adv/__init__.py` 汇出，检索实现先查该文件的导入清单。
- 高阶 API 的 Python 前端极薄：可选参数判空 → `_mat` 物化标量 → `create_asc_XxxOp` 建一个 IR 操作，多步计算在 Ascend C 内联实现中展开；math.py 用 `math_op_impl` + builder 方法名字符串把几十个函数量产成三行一组。
- 归约类高阶 API（rmsnorm）需要 tiling 结构体当「切分说明书」：`RmsNormTiling` 12 个字段与 Ascend C 结构体一一镜像，倒数等可在编译期算好的量由 Host 侧直接填好。
- TBuf 的两种典型复用：多步计算的**草稿缓冲**（gelu 的 tmp，串行九步反复写）与**常驻常量**（rmsnorm 的 gamma，一次加载全程只读）；TBuf 免队列纪律但免不了同步——草稿步间用 `pipe_barrier(PIPE_V)`，直载常量跨流水线要补 `set_flag/wait_flag(MTE2_V)`。
- 融合算子三种模式：A 手工组合（gelu）、B 一步融合 API（swiglu，用户零手工同步）、C 高阶 API + tiling（rmsnorm）；Host 侧外壳统一为「推 launch 参数 → 补齐 → 下发 → 对 torch 校验」。
- 与 01_add 的手动风格相比，TPipe/TQue 框架化风格把跨流水线同步收进 enque/deque，用户只在队列覆盖不到处手工补屏障——算子越复杂，框架化收益越大。

## 7. 下一步学习建议

- 下一讲 u7-l3「Host 侧库封装：lib/host 与 tiling 辅助」将讲解 Matmul 等高阶 API 背后的 Host 侧 tiling 计算如何经 ProxyMeta/Loader 代理到 C++ 实现，与本讲的 `RmsNormTiling`（kernel 内构造）形成「Host 算 tiling vs Device 算 tiling」的对照。
- 若想深究 `asc.adv.swiglu` 背后的 IR 与发射：用 `PYASC_DUMP_PATH` 导出 07 示例的 `codegen.mlir`，按 u5-l1 的「四名合一」反查法找到 SwiGLU Op 的 td 定义，再看发射层如何拼出 C++ 调用（u6-l5）。
- 想练习模式 A 的读者可以实现 SiLU 或 GELU 的 erf 精确形式（`0.5x(1+erf(x/√2))`，erf 在 adv/math.py 导出清单中），对比 tanh 近似版的精度差异。
- 建议顺带阅读 `python/asc/language/adv/quantization.py` 与 `sort.py`，它们是高阶 API 命名空间里另外两类（量化、排序/抽取），读码方法与本讲完全相同。
