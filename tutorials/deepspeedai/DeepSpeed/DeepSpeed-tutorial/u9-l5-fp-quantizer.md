# 低比特量化内核：FP_Quantizer、QuantizedLinear 与 FP8 GEMM

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 DeepSpeed 权重量化栈的四层结构：`QuantizedParameter`/`QuantizedLinear`（驻留层）→ `FP_Quantize`（Python 前端）→ `csrc/fp_quantizer` CUDA 内核（位级打包/解包）→ `fp8_gemm_triton`（融合反量化的矩阵乘）。
2. 解释「权重以量化形态驻留 GPU、前向时按需反量化」这套设计如何把权重显存从 bf16 的 2 字节/元素降到 1 字节（fp8）乃至 0.5 字节（fp4）。
3. 手算分组缩放（group scaling）的数学：每组求最大绝对值，缩放到目标浮点格式的量程内，scale 以 fp32 存储在每组尾部。
4. 读懂 E4M3 → fp16 的位布局对齐技巧：为什么把 7 位有效载荷左移 7 位、再乘 256，就能零成本反量化。
5. 理解一组近期修复为何让这条路径首次值得系统讲解：q_bits=12 尾数位 4→7、Python/C++ 双侧 group_size 校验、空张量零组早退、`.to()` 重建子类、E4M3 位布局修正、Triton 共享内存不足时自适应降 `num_stages`。

本讲依赖 u9-l1（OpBuilder 与 JIT 编译）和 u9-l2（CUDA 算子三件套的阅读方法）。

## 2. 前置知识

### 2.1 为什么只量化权重

大模型推理/训练中显存的大头是权重矩阵。以一个 7B 模型为例，bf16 权重占约 14 GB；若以 fp8（1 字节/元素）驻留，降到约 7 GB；fp4 再减半。LoRA 等参数高效微调方法冻结了基座权重，冻结权重的梯度与优化器状态都不存在，因此「权重压缩」是纯赚显存、不引入训练状态复杂度的选择——这正是 DeepSpeed 把量化层放进 `deepspeed/linear`（与 LoRA 同一个包）的原因。

代价是每次前向都要把低比特权重反量化回 bf16 参与计算。本讲的两条执行路径——独立 CUDA 内核反量化、以及与 GEMM 融合的 Triton 反量化——就是围绕这个代价做的优化。

### 2.2 浮点格式速览

一个二进制浮点数由三段位组成：

\[ \text{value} = (-1)^{s} \times 2^{e - \text{bias}} \times 1.m \]

- \(s\)：符号位，1 位；
- \(e\)：指数域（位宽 \(E\)），以「偏置 bias」编码，正常数 bias \(= 2^{E-1}-1\)；
- \(m\)：尾数域（位宽 \(M\)），正常数有隐含前导 1（\(1.m\)）。

当指数域为全 0 时是「次正规数」（subnormal）：值 \(= (-1)^s \times 0.m \times 2^{1-\text{bias}-M}\)，步长均匀，用于渐进下溢到 0。本讲涉及的格式：

| 格式 | 总位数 | 指数位 | 尾数位 | bias | 最大正常数 |
|---|---|---|---|---|---|
| fp16 | 16 | 5 | 10 | 15 | 65504 |
| bf16 | 16 | 8 | 7 | 127 | ≈3.39e38 |
| E4M3 | 8 | 4 | 3 | 7 | 480 |
| E4M7 | 12 | 4 | 7 | 7 | 510 |
| E3M2 | 6 | 3 | 2 | 3 | 28 |
| E2M1 | 4 | 2 | 1 | 1 | 6 |

其中 E4M3 的最大正常数 \(480 = 2^{15-7} \times (1 + 7/8)\)（该格式指数域最大值 15 不保留给 inf），E4M7 的 \(510 = 2^{8} \times (1 + 127/128)\)。这两个数字会以 `q_range` 常量的形式出现在内核里，是判断「格式是否被正确配置」的锚点。

### 2.3 分组缩放

低比特格式动态范围极窄（E4M3 只有约 \(10^1\) 量级的可用区间）。若整个张量共用一个缩放因子，离群值（outlier）会把其余元素挤成 0。分组缩放把张量按连续 `group_size` 个元素切组，每组独立计算：

\[ \text{scale}_g = \frac{\max_i |x_i|}{q\_range}, \qquad q(x_i) = \text{float-quantize}\left( \frac{x_i}{\text{scale}_g} \right) \]

反量化即 \( \hat{x}_i = \text{float-decode}(q_i) \times \text{scale}_g\)。组越小精度越高、scale 开销越大（每组固定 4 字节 fp32）。

### 2.4 你需要熟悉的三个工具概念

- **`nn.Parameter` 子类**：PyTorch 允许继承 `nn.Parameter` 并挂自定义属性（如量化器）。难点在于 `Tensor.to()` 等操作可能丢失子类身份，需要手工重建（4.1 节）。
- **torch 扩展算子**：`FPQuantizerBuilder().load()` 在运行期 JIT 编译并加载 `csrc/fp_quantizer` 的 `.so`（机制见 u9-l1）；Python 前端只是薄壳。
- **Triton**：Python 写 GPU 内核的编译器。本讲的 fp8 GEMM 内核用 `tl.load`/`tl.dot` 写成，`BLOCK_SIZE_*` 是编译期常量。

## 3. 本讲源码地图

| 文件 | 层次 | 作用 |
|---|---|---|
| [deepspeed/linear/config.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/config.py) | 配置 | `QuantizationConfig`：q_bits、mantissa_bits、group_size、q_dtype 四个旋钮 |
| [deepspeed/linear/quantization.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py) | 驻留层 | `QuantizedParameter`（参数子类，量化态驻留）与 `QuantizedLinear`（前向按需反量化） |
| [deepspeed/ops/fp_quantizer/quantize.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py) | Python 前端 | `Quantizer` 抽象基类 + `FP_Quantize`：位宽分派、缓冲分配、双侧校验、scale 视图 |
| [csrc/fp_quantizer/fp_quantize.cpp](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp) | C++ 绑定 | pybind11 入口、量程表、TORCH_CHECK 校验、空张量早退 |
| [csrc/fp_quantizer/fp_quantize_impl.cu](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu) | CUDA 内核 | 量化（分组 amax + 位手术 + 打包）与反量化（解包 + bias 恢复 + 次正规数） |
| [csrc/fp_quantizer/includes/fp_quantize.h](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/includes/fp_quantize.h) | 分发宏 | `QUANT_SWITCH`/`DEQUANT_SWITCH` 把 (位宽, 尾数) 组合映射到编译期模板实例 |
| [deepspeed/ops/fp_quantizer/fp8_gemm.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm.py) | GEMM 分派 | 有 Triton 用融合内核，否则退化为「反量化 + `torch.matmul`」 |
| [deepspeed/ops/fp_quantizer/fp8_gemm_triton.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py) | Triton 内核 | E4M3 位对齐反量化 + 分块 GEMM + 共享内存自适应降级 |
| [op_builder/fp_quantizer.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/op_builder/fp_quantizer.py) | 构建器 | Ampere+ 架构过滤、`-lcurand`（随机舍入用）、量程表镜像 |
| [tests/unit/linear/test_quant_param.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/linear/test_quant_param.py) | 测试 | 参数子类行为契约：dtype 限制、迁移量化、HF 克隆、不完整分组拒绝 |
| [tests/unit/ops/fp_quantizer/test_fp_quant.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp_quant.py) | 测试 | 以 qtorch 为外部参考验证 8/6/12-bit 量化误差与零/次正规数保真 |
| [tests/unit/ops/fp_quantizer/test_fp8_gemm.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp8_gemm.py) | 测试 | Triton fp8 GEMM 与参考 matmul 的一致性、零/次正规数精确解码 |

## 4. 核心概念与源码讲解

### 4.1 QuantizedParameter 与 QuantizedLinear：权重以量化形态驻留

#### 4.1.1 概念说明

`QuantizedParameter` 是 `nn.Parameter` 的子类，代表「一个以低比特打包形态驻留在 GPU 上的冻结权重」。它的三个关键性质：

1. **量化发生在搬运时**：CPU 上它就是普通 bf16 张量；一旦 `.to(cuda)`（或直接在 GPU 上构造），`_ensure_quantized` 就地把它替换成打包后的 uint8 缓冲。这带来一个初看很反直觉的现象——量化之后 `param.data` 的形状不再是逻辑形状（如 `(128, 128)`），而是打包缓冲形状 `(组数, 每组字节数)`。
2. **前向按需反量化**：`dequantized()` 返回还原后的 bf16 张量，原始打包数据不动。
3. **只支持冻结权重**：`requires_grad=True` 直接抛错——量化-反量化路径没有定义梯度。

`QuantizedLinear` 则是把这样一个参数装进 `nn.Linear`，前向时用 `F.linear(input, self.weight.dequantized(), self.bias)`。目前它 `assert dtype == torch.bfloat16`，即整层只支持 bf16。

#### 4.1.2 核心流程

量化参数的完整生命周期：

```
构造（CPU）                    搬运 .to(cuda)                     前向
┌──────────────┐   ┌──────────────────────────┐   ┌─────────────────────┐
│ bf16 数据     │ → │ super().to() 得到 cuda 张量│ → │ dequantized():      │
│ + quantizer  │   │ 重建 QuantizedParameter 子类│   │  CUDA 内核解包       │
│ + config     │   │ 回填 __dict__              │   │  还原逻辑形状 bf16   │
└──────────────┘   │ _ensure_quantized: 量化    │   └─────────────────────┘
                   │  .data ← (组数, 组字节) uint8│
                   └──────────────────────────┘
```

如果参数一开始就在 GPU 上构造，量化发生在 `__new__` 末尾的 `_ensure_quantized` 里——同一个入口，两种触发时机。

#### 4.1.3 源码精读

先看构造：

```python
def __new__(cls, data=None, requires_grad=False, quantization_config=None, quantizer=None):
    if requires_grad:
        raise ValueError("requires_grad=True is not supported with QuantizedParameter")
    if data is None:
        data = torch.empty(0)
    self = torch.Tensor._make_subclass(cls, data, requires_grad)
    self.quantization_config = QuantizationConfig() if quantization_config is None else quantization_config
    if quantizer is not None:
        self.quantizer = quantizer
    else:
        self.quantizer = FP_Quantize(quantization_config=self.quantization_config)
    self._ensure_quantized(self)
    return self
```

这段 [deepspeed/linear/quantization.py:L37-L56](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py#L37-L56) 是参数的诞生地：拒绝可训练参数（L44-45），用 `torch.Tensor._make_subclass` 创建子类实例（L48），把量化器对象直接藏在参数自身上（L50-54）。注意 `quantizer` 这个可选参数不是装饰——docstring 明说它服务于「用 `__dict__` 克隆参数」的场景，见下面的 HF 克隆问题。

`_ensure_quantized` 与 `dequantized`：

```python
def _ensure_quantized(self, tensor):
    if get_accelerator().on_accelerator(tensor) and tensor.dtype != self.quantization_config.q_dtype:
        with get_accelerator().stream(get_accelerator().current_stream(tensor.device)):
            tensor.data = self.quantizer.quantize(tensor.data, q_bits=..., q_mantisa_bits=...)
        assert tensor.dtype == self.quantization_config.q_dtype

def dequantized(self):
    if get_accelerator().on_accelerator(self.data) and self.data.dtype == self.quantization_config.q_dtype:
        with get_accelerator().stream(get_accelerator().current_stream(self.data.device)):
            return self.quantizer.dequantize(self.data, q_bits=..., q_mantisa_bits=...)
    return self.data
```

[deepspeed/linear/quantization.py:L58-L76](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py#L58-L76)：量化与反量化都在加速器的当前流上执行（与训练计算正确排序）；`dequantized` 的兜底分支是优雅降级——若数据不在加速器上或还没量化，直接返回原数据，所以「未量化的 QuantizedParameter」仍然可用。

**`.to()` 为什么要重建子类**（本周期修复之一）：

```python
def to(self, *args, **kwargs):
    tensor = super().to(*args, **kwargs)
    if not isinstance(tensor, type(self)):
        tensor = torch.Tensor._make_subclass(type(self), tensor, self.requires_grad)
    tensor.__dict__.update(self.__dict__)
    self.quantizer.to(*args, **kwargs)
    tensor._ensure_quantized(tensor)
    return tensor
```

[deepspeed/linear/quantization.py:L118-L129](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py#L118-L129)。`super().to()` 搬运张量后，返回值可能不再是 `QuantizedParameter`（子类身份与挂在实例 `__dict__` 里的 `quantizer`、`quantization_config` 一并丢失——一个丢了量化器的「量化参数」，之后既不能反量化也不能再次搬运量化）。修复分三步：检测类型（L124）、`_make_subclass` 重建子类（L125）、`__dict__.update` 回填量化器与配置（L126）。

这个修复与 HuggingFace 集成相关：部分模型代码会绕过 `__new__`、用 `QuantizedParameter(data, **param.__dict__)` 的方式克隆参数——`__dict__` 里带着 `quantizer` 才能让克隆体继续工作。该契约由测试固化：

```python
# should be able to clone parameter via dict, HF expects this to work
qp_copy = QuantizedParameter(qp.data, **qp.__dict__)
```

见 [tests/unit/linear/test_quant_param.py:L50-L61](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/linear/test_quant_param.py#L50-L61)（`test_hf_clone`），它同时断言克隆体的数据与量化配置逐项相等。

`QuantizedLinear`：

```python
def __init__(self, input_dim, output_dim, bias=False, quantization_config=None, dtype=torch.bfloat16):
    super().__init__(input_dim, output_dim, bias=bias, dtype=dtype)
    assert dtype == torch.bfloat16, "currently only supports bfloat16 dtype"
    self.weight = QuantizedParameter(self.weight.data, quantization_config=quantization_config)

def forward(self, input):
    return F.linear(input, self.weight.dequantized(), self.bias)
```

[deepspeed/linear/quantization.py:L139-L150](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py#L139-L150)。上游入口是 `OptimizedLinear`：只给量化配置时分发到 `QuantizedLinear`，LoRA + 量化同时开时由 `LoRAOptimizedLinear` 把基座权重包成 `QuantizedParameter`，见 [deepspeed/linear/optimized_linear.py:L66-L73](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/optimized_linear.py#L66-L73) 与 [deepspeed/linear/optimized_linear.py:L109-L113](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/optimized_linear.py#L109-L113)。

配置侧只有一个 dataclass：

```python
@dataclass
class QuantizationConfig:
    q_bits: int = 8
    mantissa_bits: int = 3
    group_size: int = 512
    q_dtype: torch.dtype = torch.uint8
```

[deepspeed/linear/config.py:L38-L54](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/config.py#L38-L54)。注意 docstring 里的说明：CUDA 上缓冲按 uint8 分配，但内核里真正的量化目标是 fp8 等浮点格式（uint8 只是打包容器）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「迁移即量化」与「前向即反量化」，并观察打包后的 `data` 形状。

操作步骤（以下为示例代码，需一块 NVIDIA GPU；结果待本地验证）：

```python
# practice_quantized_linear.py
import torch
import torch.nn.functional as F
from deepspeed.linear.quantization import QuantizedParameter, QuantizedLinear
from deepspeed.linear.config import QuantizationConfig
from deepspeed.ops.op_builder import FPQuantizerBuilder

device = "cuda"
cfg = QuantizationConfig(q_bits=8, mantissa_bits=3, group_size=64)
cfg.q_dtype = FPQuantizerBuilder.get_default_quant_dtype()  # torch.uint8

# 1) 量化参数：CPU 构造，搬运时量化
w = torch.randn(128, 128, dtype=torch.bfloat16)
qp = QuantizedParameter(w, quantization_config=cfg)
print("before:", qp.dtype, qp.data.shape)      # 期望: torch.bfloat16 torch.Size([128, 128])
qp = qp.to(device)
print("after: ", qp.dtype, qp.data.shape)      # 期望: torch.uint8 torch.Size([256, 68])
# 16384 元素 / 64 每组 = 256 组；每组 64*8/8 + 4(scale) = 68 字节

# 2) 反量化误差
w_hat = qp.dequantized()
rel = ((w_hat - w.to(device)).abs() / w.to(device).abs().clamp(min=1e-3)).mean()
print("平均相对误差:", rel.item())

# 3) QuantizedLinear 前向
layer = QuantizedLinear(128, 128, quantization_config=cfg)
layer.weight = layer.weight.to(device)   # 显式走 QuantizedParameter.to，触发量化
x = torch.randn(8, 128, dtype=torch.bfloat16, device=device)
out = layer(x)
ref = F.linear(x, w.to(device))
print("前向最大绝对差:", (out - ref).abs().max().item())
```

需要观察的现象与预期结果（待本地验证）：

1. `qp.data.shape` 从 `(128,128)` 变成 `(256,68)`——`data` 已是「组数 × 组字节」的打包缓冲，这是理解后续内核寻址的钥匙。
2. 反量化平均相对误差在百分之几以内（E4M3 只有 3 位尾数，单元素相对量化步长可达 \(2^{-3}\) 量级；分组缩放保证误差按组内相对幅度分布）。
3. 前向输出与 bf16 参考 `F.linear` 的差异与第 2 步的量化误差同量级。
4. 附加实验：把 `layer.to(device)`（模块级搬运）与 `layer.weight.to(device)`（参数级搬运）对比，观察前者的 `weight.dtype` 是否仍是 bf16——`nn.Module._apply` 默认走 `param.data` 原地替换路径，不会调用参数自己的 `.to()` 覆写，因此模块级搬运可能不触发量化（`dequantized()` 的兜底分支会让前向仍然正确，只是显存没省）。这一点依赖 torch 版本行为，请以实际观察为准。

无 GPU 时的替代实践（源码阅读型）：通读 [tests/unit/linear/test_quant_param.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/linear/test_quant_param.py)，把六个测试各自固化的契约写成一张表（dtype 限制 / requires_grad 禁止 / 迁移量化 / HF 克隆 / 小模型精确输出 / 不完整分组拒绝）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `QuantizedParameter.__new__` 要把 `quantizer` 作为可选构造参数，而不是总在内部新建 `FP_Quantize`？

答案：因为 HuggingFace 等框架会用 `QuantizedParameter(data, **param.__dict__)` 的方式克隆参数——此时 `__dict__` 展开后 `quantizer` 成为关键字参数传入。若不接受该参数，克隆体要么报「未知参数」错误，要么拿到一个全新的空量化器（丢失 `orig_dtype`/`orig_shape` 等状态而无法反量化）。契约由 `test_hf_clone` 固化。

**练习 2**：`qp.dequantized()` 在什么情况下返回的不是反量化结果？

答案：两个兜底条件（[deepspeed/linear/quantization.py:L71-L76](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/quantization.py#L71-L76)）：数据不在加速器上（还在 CPU，从未量化），或数据 dtype 不等于 `q_dtype`（例如模块级 `.to()` 只换了 data 未触发量化）。两种情况都直接返回 `self.data`，保证未量化状态下前向仍然正确。

**练习 3**：`test_small_parameter_model_output` 用 `[[1,1,1,1],[-1,-1,-1,-1],[1,-1,1,-1],[0,0,0,0]]` 这样的权重断言 `rtol=0, atol=0` 的逐位相等。为什么这种权重能做到零误差？

答案：见 [tests/unit/linear/test_quant_param.py:L63-L76](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/linear/test_quant_param.py#L63-L76)。权重只含 0、±1。每组 4 个元素（group_size=8 且 16 个元素分两组，每组内最大绝对值为 1），scale \(= 1/q\_range\)；缩放后 ±1 变成 ±480，恰是 E4M3/E4M7/E3M2 的最大正常数（可精确表示），0 是精确码。因此量化-反量化全程无舍入，输出逐位一致。这测试同时覆盖 q_bits = 6/8/12 三种格式。

### 4.2 FP_Quantize 前端：位宽分派、打包布局与双侧校验

#### 4.2.1 概念说明

`FP_Quantize` 是量化器的 Python 前端，职责是：

1. 定义可插拔接口（`Quantizer` 抽象基类），`QuantizedParameter` 的 docstring 明确说任何实现该接口的量化器都能替换 `FP_Quantize`；
2. 惰性 JIT 加载 CUDA 扩展（首次构造时 `FPQuantizerBuilder().load()`）；
3. 按 `q_bits` 把请求分派到正确的内核格式，分配打包缓冲；
4. 在把数据交给 CUDA 之前做**Python 侧校验**，C++ 侧再用 `TORCH_CHECK` 防守一道——即「双侧校验」。

打包布局是这一层的核心产物：量化输出是一个二维 uint8 缓冲，每行一个组：

\[ \text{每行字节数} = \left\lfloor \frac{\text{group\_size} \times q\_bits}{8} \right\rfloor + 4 \]

多出的 4 字节存放该组的 fp32 scale（由内核写入行尾）。

#### 4.2.2 核心流程

`quantize(input, q_bits)` 的流程：

```
1. 断言 input 为 bf16（当前唯一支持的输入格式）
2. _validate_shape：三条合法性检查（见下）
3. 记录 orig_dtype / orig_shape（dequantize 的必要状态）
4. 按 q_bits 覆盖尾数位：8→保持、12→7、6→2、4→1
5. 分配 input_q：torch.ones(num_groups, group_size*q_bits//8 + 4, uint8)
6. 调用 C++ 扩展 fp_quant_module.quantize(...)，原地填充 input_q
7. （可选）return_meta_tensor：把数据列与 scale 列切开返回
```

`dequantize(input_q, q_bits, scale=None)` 则要求先调用过 `quantize`（否则 `orig_dtype` 为 None 报错），分配 `orig_shape` 的输出缓冲后调 C++ `dequantize`；若 scale 与数据分离传入（meta tensor 路径），先按组重新拼回 `[data | scale]` 布局再进内核。

#### 4.2.3 源码精读

构造与 JIT 加载：

```python
class FP_Quantize(Quantizer):
    def __init__(self, quantization_config) -> None:
        global fp_quant_module
        super().__init__(group_size=quantization_config.group_size)
        if fp_quant_module is None:
            fp_quant_module = FPQuantizerBuilder().load()
        self.cuda_impl = getattr(fp_quant_module, "CUDA_IMPL", True)
```

[deepspeed/ops/fp_quantizer/quantize.py:L43-L56](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L43-L56)：编译产物缓存为模块级全局变量（整个进程只编译一次）；`Quantizer` 抽象基类只约定 `quantize`/`dequantize` 两个抽象方法（[deepspeed/ops/fp_quantizer/quantize.py:L17-L40](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L17-L40)）。

**Python 侧校验**（本周期新增）：

```python
def _validate_shape(self, input):
    if self.group_size <= 0:
        raise ValueError(f"quantization group_size must be positive, got {self.group_size}")
    if self.cuda_impl and self.group_size % 8 != 0:
        raise ValueError(f"quantization group_size must be divisible by 8, got {self.group_size}")
    if input.numel() % self.group_size != 0:
        raise ValueError(f"quantized tensor with {input.numel()} elements must be divisible by group_size {self.group_size}")
```

[deepspeed/ops/fp_quantizer/quantize.py:L58-L65](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L58-L65)。三条规则：group_size 为正；CUDA 实现下可被 8 整除（内核按字节打包、向量化存取的最小粒度）；元素总数被 group_size 整除（不允许半个组）。在 `quantize` 开头调用（[L77](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L77)），在分配缓冲和触碰 CUDA 之前就把非法输入挡下。

**位宽分派与 12-bit 尾数修复**：

```python
if q_bits == 8:
    pass
elif q_bits == 12:
    q_mantisa_bits = 7
elif q_bits == 6:
    q_mantisa_bits = 2
elif q_bits == 4:
    q_mantisa_bits = 1
```

[deepspeed/ops/fp_quantizer/quantize.py:L81-L91](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L81-L91)（`dequantize`/`selective_dequantize` 有完全相同的三处覆盖，见 [L126-L136](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L126-L136)）。这段覆盖把用户传的尾数位统一成内核支持的格式组合：8-bit→E4M3（保持 3）、12-bit→E4M7（7）、6-bit→E3M2（2）、4-bit→E2M1（1）。

本周期修复把 12-bit 分支的 `q_mantisa_bits = 4` 改成 `7`（三个方法同步改）。有两点值得注意：

- **量程表作证**：C++ 侧 q_bits=12 的量程是 510（[csrc/fp_quantizer/fp_quantize.cpp:L37-L41](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L37-L41)），而 \(510 = 2^{8} \times (1 + 127/128)\) 只有按 7 位尾数（E4M7）解释才成立；若尾数真是 4，最大正常数应是另一回事。旧代码声明的格式从未存在于内核模板表中。
- **分发键的巧合**：内核按 `(q_bits - q_mantisa_bits - 1) * q_mantisa_bits` 选择模板（见下面宏）。可以验证 \((12-4-1)\times4 = (12-7-1)\times7 = 28\)——两个取值选中同一个 E4M7 模板，所以旧值碰巧没有选错内核，但「声明格式（E7M4）」与「实际格式（E4M7）」已经脱节，且与测试的 qtorch 参考实现（exp_bits=4, man_bits=7，[tests/unit/ops/fp_quantizer/test_fp_quant.py:L122-L125](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp_quant.py#L122-L125)）对不上。修复让三处声明与内核、量程表、参考实现一致。

真正决定内核格式的是头文件里的分发宏：

```c
} else if (28 == Q_BITS) {                       \
    constexpr int CONST_STOCHASTIC_ROUNDING = 0; \
    constexpr int CONST_Q_BITS = 12;             \
    constexpr int CONST_Q_MANTISA_BITS = 7;      \
```

[csrc/fp_quantizer/includes/fp_quantize.h:L27-L80](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/includes/fp_quantize.h#L27-L80)（`QUANT_SWITCH`）把「指数位×尾数位+随机舍入」的组合键映射到编译期常量；`DEQUANT_SWITCH`（[L82-L106](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/includes/fp_quantize.h#L82-L106)）同理。这就是格式表：12→E4M3（8-bit, mantissa 3）、28→E4M7、10→E5M2、6→E3M2、其余→E2M1。

**缓冲分配与 C++ 入口**：

```python
self.num_groups = input.numel() // self.group_size
self.input_q = torch.ones(self.num_groups, int(self.group_size * q_bits) // 8 + 4,
                          dtype=torch.uint8, device=input.device)
out = fp_quant_module.quantize(self.input_q, input, self.group_size, stochastic_mode, q_bits, q_mantisa_bits)
```

[deepspeed/ops/fp_quantizer/quantize.py:L92-L97](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L92-L97)：正是上面布局公式 \( \lfloor \text{group\_size}\cdot q\_bits/8\rfloor + 4\) 的落点。C++ 侧入口：

```cpp
TORCH_CHECK(group_size > 0, "group_size must be positive");
TORCH_CHECK(group_size % 8 == 0, "group_size must be divisible by 8");
TORCH_CHECK(total_elems % group_size == 0, "tensor element count must be divisible by group_size");
...
int num_groups = total_elems / group_size;
// Empty tensors have no output storage and require no kernel launch.
if (num_groups == 0) { return out; }
```

[csrc/fp_quantizer/fp_quantize.cpp:L25-L52](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L25-L52)。这就是**双侧校验**的 C++ 半边：三条 `TORCH_CHECK` 与 Python `_validate_shape` 逐条对应——Python 侧给用户更早、更友好的报错，C++ 侧防守 ABI 边界（任何绕过前端直接调扩展的调用者也会被拦住）。注意 Python 的 `0 % 64 == 0` 对空张量是合法的，因此空张量会走到 C++ 侧的**零组早退**（L43-44）：`num_groups == 0` 时直接返回，不发射内核——空张量没有输出存储，launch 一个无组可处理的内核既无意义又可能踩到非法寻址。`dequantize` 与 `selective_dequantize` 有同样的三检查与早退（[L73-L79](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L73-L79)、[L109-L114](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L109-L114)）。

q_range 量程表也在这个入口：

```cpp
float q_range = q_bits == 8 ? (q_mantisa_bits == 3 ? 480.0 : 114688.0) :  // fp8 ranges
                    (q_bits == 12 ? 510.0 :                               // fp12 range
                         (q_bits == 6 ? 28.0 :                            // fp6 range
                              6.0));  // fp4 range
```

[csrc/fp_quantizer/fp_quantize.cpp:L37-L41](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L37-L41)：480/510/28/6 正是 2.2 节表格中 E4M3/E4M7/E3M2/E2M1 的最大正常数。Python 侧在 [op_builder/fp_quantizer.py:L80-L90](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/op_builder/fp_quantizer.py#L80-L90) 有一份镜像（`get_quant_range`），供测试构造参考实现。

**meta tensor 与 scale 视图**：`quantize(..., return_meta_tensor=True)`（仅限 q_bits=8）把输出按 `group_size` 切成数据列与 scale 列分别返回（[deepspeed/ops/fp_quantizer/quantize.py:L98-L106](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L98-L106)）；`get_scales` 再用 `from_blob` 把 uint8 scale 列按 fp32 重新视图（[csrc/fp_quantizer/fp_quantize.cpp:L122-L132](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L122-L132)）——这个 `(组数, 1)` 的 fp32 张量就是后面 Triton GEMM 的 `scale_ptr`。

#### 4.2.4 代码实践

**实践目标**：验证 group_size 约束在 Python 与 C++ 两侧都会拒绝非法输入，并看清报错时机。

操作步骤（示例代码，需 GPU，结果待本地验证）：

```python
# practice_validate.py
import torch
from deepspeed.linear.quantization import QuantizedParameter
from deepspeed.linear.config import QuantizationConfig

# 1) group_size=12：不满足 %8 == 0
cfg_bad = QuantizationConfig(q_bits=8, group_size=12)
qp_bad = QuantizedParameter(torch.randn(64, 64, dtype=torch.bfloat16), quantization_config=cfg_bad)
try:
    qp_bad.to("cuda")
except ValueError as e:
    print("[Python] 拒绝:", e)
# 预期: quantization group_size must be divisible by 8, got 12

# 2) 元素数不整除：5x5=25 不能被 group_size=8 整除
cfg = QuantizationConfig(q_bits=8, group_size=8)
qp2 = QuantizedParameter(torch.randn(5, 5, dtype=torch.bfloat16), quantization_config=cfg)
try:
    qp2.to("cuda")
except ValueError as e:
    print("[Python] 拒绝:", e)
# 预期: quantized tensor with 25 elements must be divisible by group_size 8

# 3) 绕过 Python 前端，直接调 C++ 扩展触发 TORCH_CHECK
from deepspeed.ops.fp_quantizer import quantize as qmod   # 步骤 1/2 的构造已 JIT 加载扩展
val = torch.randn(64, dtype=torch.bfloat16, device="cuda")
out = torch.empty(4, 68, dtype=torch.uint8, device="cuda")
try:
    qmod.fp_quant_module.quantize(out, val, 12, 0, 8, 3)   # group_size=12 直接进 C++
except RuntimeError as e:
    print("[C++] 拒绝:", e)
# 预期: group_size must be divisible by 8
```

需要观察的现象与预期结果：

1. 前两步在**分配缓冲之前**就抛 `ValueError`（对照 `test_rejects_incomplete_quantization_group`，[tests/unit/linear/test_quant_param.py:L78-L87](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/linear/test_quant_param.py#L78-L87)）。
2. 第三步证明 C++ `TORCH_CHECK` 独立生效——即使没有 Python 前端把关，扩展自身也会拒绝。
3. 把第三步换成 `torch.empty(0)` 的空输入与合法 group_size，确认走的是零组早退（不报错、返回原样）而非崩溃。待本地验证。

无 GPU 替代：阅读 [deepspeed/ops/fp_quantizer/quantize.py:L58-L77](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L58-L77) 与 [csrc/fp_quantizer/fp_quantize.cpp:L33-L44](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize.cpp#L33-L44)，逐条画出「Python 检查 ↔ TORCH_CHECK」的对应表。

#### 4.2.5 小练习与答案

**练习 1**：`group_size % 8 == 0` 这条约束为什么只对 `cuda_impl` 生效（`if self.cuda_impl and ...`）？

答案：这是 CUDA 内核的实现约束而非数学约束。内核按字节粒度打包位流（每 8 个元素恰好占整数个字节）并以 2/4 字节粒度做向量化全局存取（`quanitzed_access_granularity` 常量，见 [csrc/fp_quantizer/fp_quantize_impl.cu:L28-L33](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L28-L33)）；非 8 倍数的组无法对齐这些存取。`cuda_impl` 标记当前加载的是 CUDA 扩展，纯 Python/其他后端路径不受此限。

**练习 2**：`input_q` 为什么初始化为 `torch.ones` 而不是 `torch.empty`？

答案：这是一道开放观察题——内核会覆写全部数据位与 scale 位，初始化值理论上不影响正确输出；`ones`（而非 `empty`）保证了未被覆写的字节（若因掩码边界残留）是确定值 1 而非未定义内存，让结果可复现、便于调试。可以在源码中确认内核对每组 `group_size*q_bits/8 + 4` 字节是全量写入的（数据位循环 + 行尾 scale，见 4.3 节）。

**练习 3**：`dequantize` 为什么要求「先调过 `quantize`」，而 GEMM 路径又能用分离的 `scale` 参数？

答案：`dequantize` 需要 `orig_dtype`/`orig_shape` 两个只在 `quantize` 时记录的状态（[deepspeed/ops/fp_quantizer/quantize.py:L121-L125](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L121-L125)），且默认从打包缓冲行尾读 scale。分离 scale 的入口是 `scale=` 参数：此时输入只是数据位（uint8，元素数等于输出），函数会把它与 scale 重新按组拼接成 `[data | scale]` 再进内核（[L138-L142](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/quantize.py#L138-L142)）——GEMM 的 fallback 路径（先反量化再 `torch.matmul`）正是走这条路。

### 4.3 csrc fp_quantize CUDA 内核：分组归约、位手术与次正规数

#### 4.3.1 概念说明

CUDA 内核做两件事：`apply_quantization`（bf16 → 打包位流）与 `apply_dequantization`（打包位流 → bf16/fp16）。量化内核的组织方式是 **warp-per-group**：每个 warp 负责一个组，先在组内做块级最大值归约得到 amax，再对每个元素做「位手术」——直接在 fp32 位模式上截取符号/指数/尾数域，重排到目标格式的位宽——最后把结果位打包进输出缓冲。

「位手术」比「浮点除法 + cast」快，但绕过了硬件的舍入逻辑，所以次正规数（指数域 0）和进位边界必须手工处理——这正是本周期「零与次正规数修复」的战场。

#### 4.3.2 核心流程

`apply_quantization` 每个 warp 的执行流程：

```
1. 向量化加载本组 bf16 元素（16 字节粒度），同时归约组内 |max|
2. scale = q_range / max        （缩放因子，注意方向：把值放大进格式量程）
3. 对每个元素:
   val_f = float(x) * scale      →  按位 reinterpret 成 uint32
   取 sign / exponent / mantissa 三个位域
   若原指数 < 目标格式最小正常指数（次正规区间）:
       shifted = |val_f| + q_min_normal; 取其尾数域 → 均匀步长的次正规码
   否则: round（尾数舍入，可选随机舍入）→ clip（夹到格式量程）→ 指数重定偏置
   按 q_bits 打包进 64 位缓冲（12-bit 特殊：跨 64 位边界拼接）
4. warp 首线程把 scale 的 fp32 字节写进本组行尾 4 字节
```

`apply_dequantization` 是逆过程：按组寻址读出 scale 与数据位，指数域加回偏置差还原成 fp16/bf16 位模式（次正规数走 \(m \times \text{step}\) 的显式乘法），最后乘 scale。

#### 4.3.3 源码精读

组内归约与缩放因子：

```cpp
reduce::_block<T, 1, ROp::Max>(tb, warp, &cur_max);
...
float scale = (float)q_range / conversion::to<float>(cur_max);
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L110-L125](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L110-L125)：先跨线程块求组内最大绝对值（L110-119 的加载与归约），再算 \( \text{scale} = q\_range / \max\)。每个元素乘 scale 后落在 \((-\,q\_range,\ q\_range]\) 内，即目标格式的满量程。行尾写入的是反方向 \( \max / q\_range\)（[L222-L223](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L222-L223)：`float q_scale = conversion::to<float>(cur_max) / (float)q_range;`），反量化时直接相乘即可。

位手术主体：

```cpp
float val_f = conversion::to<float>(tmp_buf[...]) * scale;
uint32_t* data = reinterpret_cast<uint32_t*>(&val_f);
uint32_t sign = (data[0] & _sign_mask) >> (_mantisa_bits + _exponent_bits);
uint32_t cur_exponent = (data[0] & _exponent_mask) >> _mantisa_bits;
uint32_t dst_mantisa = (data[0] & _mantisa_mask);
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L133-L137](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L133-L137)：把 fp32 当整数看待，用三个掩码直接切出符号/指数/尾数（`_mantisa_bits=23, _exponent_bits=8` 是 fp32 的域宽，由模板传入）。

**次正规数修复**（本周期，让零和极小值不再丢失）：

```cpp
if (cur_exponent < q_min_normal_exponent) {
    // Zero and values below the smallest normal use exponent field 0, whose
    // steps are uniform like IEEE subnormals. Adding the smallest normal fixes
    // the exponent, so the shared rounding lands on that step; a carry means
    // the value rounded up to the smallest normal (field 1).
    float shifted = fabsf(val_f) + q_min_normal;
    dst_mantisa = __float_as_uint(shifted) & _mantisa_mask;
    dst_exponent = q_min_normal_exponent;
    round<_mantisa_bits, q_mantisa_bits, stochastic_rounding>(dst_mantisa, dst_exponent, &state);
    dst_exponent -= q_min_normal_exponent;
}
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L141-L151](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L141-L151)。难点：目标格式指数域为 0 时值是均匀步长的次正规码，而直接截尾数域得到的是「1.m 隐含前导 1」的正常数布局，两者语义不同。技巧是给 \(|val|\) 加上目标格式的最小正常数 \(q\_min\_normal = 2^{1-\text{bias}-M} \times \text{基数}\)：和的浮点表示有合法指数域，其尾数域的低若干位恰好就是 \(|val|\) 按 \(2^{1-\text{bias}-M}\) 步长取整的结果——一次浮点加法同时完成了「缩放到次正规网格 + 舍入」。修复前直接走正常数路径，零和小值会被错误编码。舍入后的进位（加法溢出到最小正常数）由 `dst_exponent -= q_min_normal_exponent` 自然落回指数域 1。

正常数路径的舍入与夹取由两个小模板完成（[L36-L60](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L36-L60)）：`round` 在被截掉的位上加半位（或用 curand 泊松随机数做随机舍入——这是构建器链 `-lcurand` 的用途，[op_builder/fp_quantizer.py:L66-L70](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/op_builder/fp_quantizer.py#L66-L70)）；`clip` 把超出目标格式指数范围的值夹到边界。随后指数域重定偏置（[L158-L160](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L158-L160)）：减 fp32 的 bias 127、加目标格式的 bias。

**打包与 scale 存储**：

```cpp
q_buf = q_buf | ((uint64_t)((uint8_t)(sign << (q_exponent_bits + q_mantisa_bits) |
                                      (dst_exponent << q_mantisa_bits) | dst_mantisa))
                 << j * total_q_bits);
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L164-L189](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L164-L189)：每个元素装配成「符号|指数|尾数」的整数后按 `total_q_bits` 移位累进 64 位缓冲；8/6/4-bit 单缓冲即可，12-bit 需要 `q_buf1` 两级拼接（5 个 12-bit 元素占 60 位，第 6 个跨界）。行尾 scale 写入：

```cpp
if (lane == 0) {
    float q_scale = conversion::to<float>(cur_max) / (float)q_range;
    uint32_t scale_offset = gid * ((group_size * total_q_bits / 8) + 4) + (group_size * total_q_bits / 8);
    ...store_global...(q_val + scale_offset, scale_as_int8);
}
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L222-L236](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L222-L236)：`scale_offset` 精确对应 Python 分配的「+4」字节——组数据位之后紧贴 fp32 scale，量化输出因此是自描述的（反量化不需要额外元数据）。

反量化内核的解码：

```cpp
if (dst_exponent == 0) {
    up_cast = dst_mantisa * q_subnormal_step;   // 次正规数：均匀步长乘法
    if (sign) up_cast = -up_cast;
} else {
    dst_exponent = (dst_exponent - ((1 << (q_exponent_bits - 1)) - 1)) + (1 << (dst_exponent_bits - 1)) - 1;
    uint16_t packed = ((sign << (dst_exponent_bits + mantisa_bits)) | (dst_exponent << mantisa_bits) |
                       (dst_mantisa << (mantisa_bits - q_mantisa_bits)));
    memcpy(&store_buf[j], &packed, sizeof(packed));
    up_cast = conversion::to<float>(store_buf[j]);
}
store_buf[j] = conversion::to<T>(up_cast * scale);
```

[csrc/fp_quantizer/fp_quantize_impl.cu:L311-L343](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L341)（完整段 [L311-L343](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L311-L343)）。三个看点：

1. **次正规数对称处理**：`q_subnormal_step = 2^{1-\text{bias}-M}`（[L251-L259](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L251-L259) 常量推导），与量化侧的加法技巧互逆；
2. **位模式直拼**：正常数把符号/重偏置的指数/左移后的尾数直接拼成 fp16/bf16 位模式再 `memcpy` 进类型化存储——注释特意说明用 `memcpy` 而非 uint16 覆盖是为了在 nvcc 13 下可靠（[L335-L340](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L335-L340)）；
3. 实例化分 dtype：bf16 模板尾数 7、fp16 模板尾issa 10（[L419-L425](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L419-L425)），因为反量化输出的目标格式不同。

保真契约由专门测试固化：

```python
subnormal_step = 2**(1 - bias - mantissa_bits)
values = [1.0, subnormal_step / q_range, 0.0, -subnormal_step / q_range, -1.0, 0.5, -0.5, 0.0]
...
assert x_dequantized[2] == 0        # 零保持精确零
assert x_dequantized[1] != 0        # 最小次正规步长不被冲成零
```

[tests/unit/ops/fp_quantizer/test_fp_quant.py:L137-L154](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp_quant.py#L137-L154)（`test_fp_quant_preserves_zero_and_subnormal_codes`）：构造 ±次正规步长、0、±0.5、±1 的混合序列，断言零仍是零、次正规码非零——正是对上面两条次正规路径的回归测试，参数化覆盖 E3M2/E4M3/E4M7。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不依赖 GPU，用手算验证打包布局与偏移公式，建立「能在纸上算出内核每个字节归属」的能力。

操作步骤：

1. 对 `q_bits=8, group_size=512`（默认配置）计算：一个 `(4096, 4096)` 的 bf16 权重的打包缓冲形状、scale 字节占比、显存节省比。
2. 对 `q_bits=12, group_size=512` 重复计算，体会 12-bit「1.5 字节/元素」的布局与跨 64 位边界拼接的必要性。
3. 在 [csrc/fp_quantizer/fp_quantize_impl.cu:L98-L100](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L98-L100) 找到 `base_store_offset` 的表达式，代入 `gid=3, group_size=512, q_bits=8, lane=0`，写出该 warp 数据位起始字节与本组 scale 起始字节。
4. 对照 `test_fp_quant_preserves_zero_and_subnormal_codes`，解释为什么锚定值取 `subnormal_step / q_range`（提示：乘 scale 后恰好等于一个最小步长）。

预期结果（可立即核对）：

1. \(4096^2 = 16777216\) 元素，32768 组；每组 \(512\times8/8+4 = 516\) 字节 → 缓冲 `(32768, 516)`，约 16.9 MB；bf16 原始 33.6 MB → 节省约 49.7%（scale 占每组 4/516 ≈ 0.78%）。
2. 12-bit：每组 \(512\times12/8+4 = 772\) 字节，缓冲约 25.3 MB，节省约 24.8%——12-bit 的压缩比明显劣于 8-bit，换来的是 E4M7 的更高精度。
3. `gid=3`：数据位起始 \(3\times516 = 1548\)；scale 起始 \(3\times516+512 = 2060\)（[L222-L226](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L222-L226) 的 `scale_offset` 公式）。
4. `subnormal_step / q_range × scale = subnormal_step × (amax/q_range) × ...`——当组内 amax 使 scale 恰为 1 时（或按比例），该值落在最小可表示步长上，能区分「被冲成零」与「保住次正规码」两种行为。

#### 4.3.5 小练习与答案

**练习 1**：量化内核里 `scale = q_range / amax`，行尾存储的却是 `amax / q_range`。为什么不统一存一个？

答案：两个方向的用途不同。元素侧用 \(q\_range/\max\) 把输入**放大**到目标格式满量程（值越大、相对量化误差越小）；存储侧保存的是反量化时要乘的**还原**因子 \(\max/q\_range\)，直接对应 \(\hat{x} = \text{decode}(q)\times(\max/q\_range)\)。存后者让反量化内核只需一次乘法，且与 Triton GEMM 的 `scale_ptr` 语义一致（后者直接拿它乘回）。

**练习 2**：随机舍入（stochastic rounding）在什么场景有意义？代价是什么？

答案：正常数路径的 `round` 模板在随机模式下用 curand 泊松采样代替固定的「加半位」（[csrc/fp_quantizer/fp_quantize_impl.cu:L36-L44](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L36-L44)）。对极低比特（fp4/fp6）量化，确定性舍入会让大量小梯度/小权重系统性偏向最近格点，训练时误差累积；随机舍入让期望值无偏。代价是每次调用需要 curand 状态初始化（[L106-L108](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/csrc/fp_quantizer/fp_quantize_impl.cu#L106-L108)）与 `-lcurand` 链接，且结果不可复现——这也解释了分发宏为「同格式 ±随机舍入」各留一个模板实例（键 12/13、28/29 等）。

**练习 3**：`apply_dequantization` 中正常数为什么可以「拼位模式 + 一次浮点乘」，而次正规数必须走浮点乘法 `dst_mantisa * q_subnormal_step`？

答案：正常数有隐含前导 1，把「重偏置指数 + 左移尾数」直接放进 fp16/bf16 的对应域，位模式本身就是正确编码，只差 bias 偏移（已在指数上补偿），硬件解码免费完成。次正规数的编码是 \(0.m \times 2^{1-\text{bias}-M}\)，没有隐含 1，任何 fp16/bf16 位模式都无法直接表达「目标格式的次正规」——fp16 自己的次正规步长与目标格式的不同，所以必须显式算出浮点值 \(m \times \text{step}\) 再走类型转换。

### 4.4 fp8 Triton GEMM：E4M3 位对齐反量化与自适应启动

#### 4.4.1 概念说明

`QuantizedLinear.forward` 的「先全量反量化再 matmul」要为每次前向物化一份完整 bf16 权重，显存峰值和带宽都不划算。融合路径把反量化搬进 GEMM 内核：权重以打包 uint8 形式进入内核，每个分块在寄存器里解码成浮点、立刻参与 `tl.dot`，bf16 权重从不需要整体存在。分派逻辑很简单：Triton 可用就走融合内核，否则退化为「反量化 + `torch.matmul`」（[deepspeed/ops/fp_quantizer/fp8_gemm.py:L16-L27](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm.py#L16-L27)）。

内核的反量化核心是一个优雅的位技巧：**E4M3 的位布局可以按 fp16 的布局重新解释，只差一个统一的 256 倍因子**。

#### 4.4.2 核心流程

```
matmul_fp8(inp, weight(uint8 打包), scale(fp32 组因子), group_size, quantizer)
  ├─ Triton 可用 → matmul_fp8_triton
  │    ├─ 按 M 选 tile: BLOCK_M ∈ {16,32,64,128,(256)}, BLOCK_N ∈ {64,(128)}
  │    │   BLOCK_K = max(64 或 128, group_size)   ← 保证一个 K 块整数个组
  │    ├─ kernel: 每个 K 块加载 weight 字节与该组的标量 scale
  │    │   位技巧解码 E4M3 → bf16/fp16 → tl.dot 累加 (fp32)
  │    └─ _launch_with_fitting_num_stages: 共享内存不足则降级重试
  └─ 否则 → matmul_fp8_fallback = torch.matmul(inp, quantizer.dequantize(...))
```

scale 寻址依赖一个关键约束：`BLOCK_SIZE_K ≥ group_size` 且整除，使一个 K 块内每列只需一个 scale 标量；组索引由权重在展平张量中的线性位置除以 group_size 得到，即 `k*(N/group_size) + n/group_size`。

#### 4.4.3 源码精读

**E4M3 → fp16 位技巧**：

```python
# Dequantize weight (fp8 -> bf16)
# The E4M3 bits are laid out as fp16, whose 4 low exponent bits and subnormals line up
# with E4M3 (exponent field 0 is zero or subnormal); 2^(15 - 7) then restores the bias.
w = (weight & 0x80).to(tl.uint16) << 8
w = w | ((weight & 0x7f).to(tl.uint16) << 7)
w = (w.to(tl.float16, bitcast=True).to(tl.float32) * (scale * 256.0)).to(tl.bfloat16)
```

[deepspeed/ops/fp_quantizer/fp8_gemm_triton.py:L50-L56](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L50-L56)（bf16 内核；fp16 内核同款技巧见 [L101-L104](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L101-L104)）。推导：

- 第一行把 E4M3 的符号位（bit 7）搬到 fp16 的符号位（bit 15）；
- 第二行把低 7 位（4 位指数 + 3 位尾数）整体左移 7 位，落到 fp16 的 bit 13..7。于是 E4M3 的 4 位指数域恰好对齐 fp16 5 位指数域的**低 4 位**，E4M3 的 3 位尾数对齐 fp16 10 位尾数的**高 3 位**；
- 指数域对齐的妙处：E4M3 指数域为 0 时 fp16 指数域也是 0——两边都是「零或次正规」，语义一致。对一个指数域为 \(e\) 的正常数，fp16 读出 \(2^{e-15}\times 1.m\)，而 E4M3 本意是 \(2^{e-7}\times 1.m\)，比值恒为 \(2^{8} = 2^{15-7} = 256\)；对次正规数，fp16 读出 \(m \cdot 2^{-3} \cdot 2^{-14}\)，E4M3 本意是 \(m \cdot 2^{-3} \cdot 2^{-6}\)，比值同样是 \(2^{8}\)。所以**一个统一的 `*256` 修正全部情形**，零也严格映射为零（\(m=0\)）。
- 最后乘组因子 `scale`（即 4.3 节行尾存的 \(\max/480\)）完成还原。整个过程没有查表、没有分支，三条位运算 + 一次乘法。

这个位布局的正确性由专门测试守卫：[tests/unit/ops/fp_quantizer/test_fp8_gemm.py:L62-L89](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp8_gemm.py#L62-L89)（`test_fp8_gemm_decodes_zero_and_subnormal_weights`）在权重里植入「精确零」与「次正规码」行（用每组的 4.0 锚点把 scale 定死，使组内其它元素分别落为零和次正规区间），再用 one-hot 输入让 GEMM 输出逐行等于解码后的权重，断言 `torch.equal`——零必须精确、次正规必须非零。

**scale 寻址**：

```python
weight_ptrs_offset = offs_k[:, None] * (stride_bk // quantization_group_size) + \
                     ((pid_n * BLOCK_SIZE_N) // quantization_group_size)
...
scale = tl.load(scale_ptr + weight_ptrs_offset + ((k * BLOCK_SIZE_K * stride_bk) // quantization_group_size))
```

[deepspeed/ops/fp_quantizer/fp8_gemm_triton.py:L43-L50](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L43-L50)：权重 `(K, N)` 行主序、展平后按 group_size 分组，组号 \(= (k\cdot N + n)/\text{group\_size} = k\cdot(N/\text{group\_size}) + n/\text{group\_size}\)，正是表达式的两项。tile 选择处 `BLOCK_SIZE_K = max(64, quantization_group_size)`（[L160-L171](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L160-L171)）保证 K 块边界与组边界对齐——这是「每列每块只加载一个标量 scale」成立的前提。

**共享内存自适应降级**（本周期修复）：

```python
def _launch_with_fitting_num_stages(kernel, grid, args, num_stages, num_warps, **tile_config):
    inp = args[0]
    cache_key = (inp.device, inp.dtype, num_warps, tuple(sorted(tile_config.items())))
    num_stages = _fitting_num_stages.get(cache_key, num_stages)
    while True:
        try:
            kernel[grid](*args, num_stages=num_stages, num_warps=num_warps, **tile_config)
            break
        except OutOfResources as e:
            if e.name != "shared memory" or num_stages == 1:
                raise
            num_stages -= 1
    _fitting_num_stages[cache_key] = num_stages
```

[deepspeed/ops/fp_quantizer/fp8_gemm_triton.py:L128-L145](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L128-L145)，配套模块级缓存字典 [L18-L19](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L18-L19)。背景写在注释里：Triton 软件流水线的每个 stage 要在共享内存里保留一份输入块和权重块，首选 `num_stages` 在大 tile（如 M≥256 时 256×128）下会超过设备上限——注释举例「3 stages 需要 263168 字节，而 H100/H200 只允许 232448」。修复行为：捕获 `OutOfResources`，仅当资源名是 shared memory 时把 `num_stages` 减一再试，降到 1 仍不行才向上抛；最终拟合值按 `(设备, dtype, num_warps, tile 配置)` 缓存，同一进程内后续启动零重试成本。

另一处细节修复在 fp16 内核的预取：最后一个 K 块的预取会越界读到 scale 缓冲区之后一组，因此预取必须与权重加载用同一套边界掩码（[L109-L116](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L109-L116)，注释写明 "it addresses one group past the scale buffer"）。

一致性测试覆盖 M 从 1 到 2048 的全档位 tile：

```python
@pytest.mark.parametrize("M", [1, 2, 4, 8, 32, 64, 128, 256, 512, 1024, 2048])
def test_fp_quant(dtype, q_bits, M):
    ...
    out = matmul_fp8(x, weight, scale, quantization_group_size, fpq)
    out_q = torch.matmul(x, _dequantize_as(fpq, weight, dtype))
    error = ((out - out_q).abs() / (out.abs() + 1e-5)).sum() / out.numel()
    assert 0.004 > error
```

[tests/unit/ops/fp_quantizer/test_fp8_gemm.py:L33-L59](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp8_gemm.py#L33-L59)：参考实现刻意让 dequantize 直接落到激活 dtype（`_dequantize_as`，[L27-L30](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/ops/fp_quantizer/test_fp8_gemm.py#L27-L30)）——因为 GEMM 内核对每个解码权重只做一次「到激活 dtype」的舍入，参考若先经 bf16 再转 fp16 会多一次舍入、误差对不上。这个细节本身就是一个好的教学点：**融合内核改变了数值路径，参考实现必须沿着同一条路径构造**。

#### 4.4.4 代码实践

**实践目标**：验证 Triton fp8 GEMM 与参考 matmul 的一致性，并观察 tile/scale 布局约束。

操作步骤（需 NVIDIA GPU + Triton，结果待本地验证）：

1. 跑现成测试：`pytest tests/unit/ops/fp_quantizer/test_fp8_gemm.py -v`（依赖 `qtorch` 已在 `requirements/requirements-dev.txt` 声明，必要时 `pip install -e ".[dev]"`）。预期 22 个参数化用例（2 dtype × 11 个 M）与 4 个零/次正规用例全部通过。
2. 示例代码：手写一个对照脚本：

```python
# practice_fp8_gemm.py
import torch
from deepspeed.linear.config import QuantizationConfig
from deepspeed.ops.fp_quantizer import FP_Quantize, matmul_fp8
from deepspeed.ops.op_builder import FPQuantizerBuilder

device = "cuda"
g = 128
fpq = FP_Quantize(QuantizationConfig(q_bits=8, group_size=g,
                                     q_dtype=FPQuantizerBuilder.get_default_quant_dtype()))
w = torch.randn(512, 256, dtype=torch.bfloat16, device=device)   # K=512, N=256
weight, _ = fpq.quantize(w.data, q_bits=8, return_meta_tensor=True)
scale = fpq.get_scales()

for M in (1, 32, 256):
    x = torch.randn(M, 512, dtype=torch.bfloat16, device=device)
    out = matmul_fp8(x, weight, scale, g, fpq)
    ref = torch.matmul(x, fpq.dequantize(weight, scale=scale))
    err = ((out.float() - ref.float()).abs() / (ref.float().abs() + 1e-5)).mean()
    print(f"M={M}: 平均相对误差 {err.item():.2e}")
```

需要观察的现象与预期结果（待本地验证）：

1. 所有 M 档位的平均相对误差远小于 0.004（测试阈值）。
2. 把 `g` 从 128 改成 96（仍满足 %8 与整除约束，`512 % 96 != 0` 会被 Python 校验拒绝——注意这里的约束是 `numel % group_size`，512×256 元素整除 96，但 `N=256` 不被 96 整除会破坏内核的 scale 寻址假设 `stride_bk // group_size`）。观察是否报错或数值异常，进而理解「Python 校验的整除约束（按元素总数）弱于 GEMM 内核的寻址约束（按行长度）」——融合 GEMM 的调用方（见 `_dequantize_as` 用例）固定用 group_size=128 且 N=8192 的组合不是偶然。
3. 第二次调用同形状 GEMM 时无重编译重试（命中 `_fitting_num_stages` 缓存）；若在小显存卡上跑 M=2048，可观察到首次调用可能触发一次降级。

无 GPU 替代（源码阅读型）：对照 [deepspeed/ops/fp_quantizer/fp8_gemm_triton.py:L37-L44](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/fp_quantizer/fp8_gemm_triton.py#L37-L44) 的标准 grouped-GEMM tile 遍历（与 Triton 官方教程同构），标出三处 DeepSpeed 特有改造：weight 按 uint8 加载、scale 标量加载、循环内解码。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BLOCK_SIZE_K = max(64, quantization_group_size)` 而不是任取 64？

答案：scale 是每 `group_size` 个连续元素共享一个标量。若一个 K 块横跨两个组，块内不同 k 行需要不同 scale，`tl.load` 单标量的寻址公式（每列一个 `weight_ptrs_offset`）就不再成立。把 BLOCK_K 取为 group_size 的整数倍（且 tile 配置里 BLOCK_SIZE_N 也整除 group_size）保证了「一个块内每列恰好一个组」，scale 加载退化为一次标量读。

**练习 2**：位技巧把 E4M3 当 fp16 解码时，E4M3 的指数域最多 15（0..15），而 fp16 指数域 5 位中此处置了 0、其余 4 位来自 E4M3。那么 fp16 的 inf/NaN 域（指数全 1，即 31）会被触发吗？

答案：不会。对齐后 fp16 指数域的高位（bit 14）恒为 0，低 4 位最多到 15，所以 fp16 指数域最大是 `01111` = 15 < 31，永远落在正常数或次正规数区间，不产生 inf/NaN。这也解释了为什么该技巧对 E4M3 安全、但不能直接推广到指数域更宽的格式（如 E5M2 需要不同的对齐与修正因子）。

**练习 3**：`_launch_with_fitting_num_stages` 为什么把缓存键设计成 `(device, dtype, num_warps, tile_config)`，而不是只按 kernel 缓存？

答案：因为共享内存占用同时取决于 tile 形状（BLOCK_M/N/K 决定每 stage 缓存的输入与权重块大小）、流水线深度（num_stages 本身）和 dtype（fp16/bf16 元素宽度），而上限取决于具体设备。同一 kernel 在不同 tile 配置或不同 GPU 上的「最大可容纳 num_stages」不同；键覆盖这四个维度后，拟合结果才能在命中时直接复用，避免每次启动都重新试错。

## 5. 综合实践

设计一个贯穿本讲的端到端小任务：**给你的量化栈写一份「体检报告」**。

任务描述（需 GPU；无 GPU 时完成源码阅读替代项）：

1. **量化精度扫描**（覆盖 4.1/4.2/4.3）：对同一个 `(1024, 1024)` bf16 随机权重，分别用 `q_bits ∈ {4, 6, 8, 12}`、`group_size ∈ {64, 512}` 量化再反量化（示例代码，仿照 4.1.4 的写法），记录每种组合的：平均绝对误差、平均相对误差、打包缓冲形状与字节总数。把结果整理成一张表，用 4.3.4 的布局公式核对字节总数，用 E4M3/E4M7/E3M2/E2M1 的尾数位宽解释误差排序。
2. **双侧校验矩阵**（覆盖 4.2）：按 4.2.4 的脚本，对 `group_size ∈ {0, 12, 8}` × `numel % group_size ∈ {整除, 不整除}` 的组合列表格记录 Python/C++ 两侧的报错与报错时机。
3. **融合 GEMM 对照**（覆盖 4.4）：取量化后的权重走 `matmul_fp8`，与「先 `dequantized()` 再 `torch.matmul`」比较误差；再用 `torch.cuda.max_memory_allocated()` 分别包住两种前向，对比峰值显存，验证「融合反量化不物化完整 bf16 权重」。
4. **回归测试通读**：跑（或通读）本讲三个测试文件，把每个测试映射回它固化的契约：

   ```bash
   pytest tests/unit/linear/test_quant_param.py tests/unit/ops/fp_quantizer/test_fp_quant.py tests/unit/ops/fp_quantizer/test_fp8_gemm.py -v
   ```

预期产出：一份包含误差表、校验矩阵、显存对比和「测试 ↔ 契约」映射的 Markdown 笔记。所有 GPU 步骤待本地验证。

无 GPU 替代（源码阅读型）：完成 4.3.4 的手算实践，并沿调用链 `QuantizedLinear.forward → QuantizedParameter.dequantized → FP_Quantize.dequantize → fp_quant_module.dequantize → apply_dequantization` 画一张带行号注释的调用链图，再沿 `matmul_fp8 → matmul_fp8_triton → matmul_kernel_fp8_bf16` 画第二张。

## 6. 本讲小结

- **四层结构**：`QuantizedParameter`/`QuantizedLinear` 负责「量化态驻留、按需反量化」的生命周期；`FP_Quantize` 是 Python 前端（位宽分派、缓冲分配、双侧校验）；`csrc` CUDA 内核做位级打包/解包；`fp8_gemm_triton` 把反量化融进 GEMM 分块。
- **打包布局**是全栈的公共契约：每行一个组，\( \lfloor \text{group\_size}\cdot q\_bits/8\rfloor + 4\) 字节，行尾 4 字节存 fp32 scale；量化后参数的 `.data` 形状就是 `(组数, 行字节数)`。
- **分组缩放**：内核按 warp-per-group 归约 amax，元素乘 \(q\_range/\max\) 进格式满量程，行尾存 \(\max/q\_range\) 供反量化一次乘法还原；量程表 480/510/28/6 与 E4M3/E4M7/E3M2/E2M1 一一对应，也是发现 q_bits=12 尾数位写错（4→7）的锚点。
- **次正规数必须显式处理**：量化侧用「加最小正常数再取尾数」的技巧落在均匀步长网格，反量化侧显式算 \(m\times 2^{1-\text{bias}-M}\)；零与次正规数的保真由专门测试守卫。
- **E4M3 位对齐技巧**：7 位有效载荷左移 7 位即成合法 fp16 位模式，正常数与次正规数统一差 \(2^{15-7}=256\) 倍，三条位运算加一次乘法完成融合 GEMM 中的按需反量化。
- **工程修复的共性**：Python 校验提前拦、TORCH_CHECK 守 ABI 边界（双侧）；空张量零组早退；`.to()` 重建子类保住量化器；Triton 按设备/tile 缓存自适应降 `num_stages`——都是「让既有设计在真实环境里可靠」的守卫性修复。

## 7. 下一步学习建议

- **沿集成点向上**：读 [deepspeed/linear/optimized_linear.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/linear/optimized_linear.py) 中 `OptimizedLinear.__new__` 的三分发（纯量化 / LoRA+量化 / 普通 Linear），理解量化与 LoRA 基座权重分片（`base_weight_sharding`）如何叠加。
- **沿推理侧对比**：`deepspeed/inference/quantization/layers.py` 里还有另一个同名 `QuantizedLinear`（基于 DeepspeedInferenceConfig 的推理量化路径），对比两套设计的取舍。
- **横向看其它 Triton 算子**：`deepspeed/ops/triton_ops/`（如 `vocab_parallel_softmax.py`，见 u6-l7）与本讲内核共享「tile 遍历 + constexpr 参数 + 运行期适配」的写法。
- **回到构建体系**：对照 u9-l1，用 `DS_BUILD_FP_QUANTIZER=1 pip install -e .` 预编译本讲算子，观察 `ds_report` 里 fp_quantizer 一行的 installed/compatible 状态翻转。
- **关注上游演进**：量化栈的测试仍在快速充实（零/次正规、不完整分组等用例都是近期加入），`git log --oneline -- deepspeed/ops/fp_quantizer/ csrc/fp_quantizer/` 是跟踪这条路径演进的最佳入口。
