# Blockscaled 操作数与格式

## 1. 本讲目标

本讲是量化系列的第一篇，聚焦 QuACK 里「块缩放量化（block-scaled quantization）」的**主机侧抽象**：格式描述符与操作数容器。学完后你应当能够：

- 说清什么是块缩放量化、为什么要把权重和 scale factor（SF）绑成一个整体交给 GEMM；
- 读懂 `BlockScaledFormat` 描述符的每一个字段，并理解它是格式的「唯一真相源」；
- 区分 MXFP8 / MXFP4 / NVFP4 / MXFP6 四类格式的差异，尤其是 **SF 向量长度（`sf_vec_size`）** 与 **scale dtype** 的不同；
- 用 `BlockScaledOperand.quantize` / `from_parts` 构造量化操作数，并解释它如何被 GEMM 接口消费。

本讲只讲**主机侧容器与格式**，不涉及 SM100 设备侧 tcgen05 MMA 内核细节（那是 u5-l3、u7-l2 的内容），也不讲量化输出 epilogue（u6-l5 已覆盖）。

## 2. 前置知识

### 2.1 块缩放量化（Microscaling, MX）

普通 fp8/fp4 量化是「整个张量共用一个 scale」。**块缩放**则把收缩轴（GEMM 的 K 轴）切成大小为 `sf_vec_size` 的小块，**每个块共用一个 scale factor** \(s_b\)。量化与反量化：

\[
\hat{x}_i = \mathrm{round}(x_i / s_b), \qquad i \in \text{block } b
\]

\[
x_i \approx \hat{x}_i \cdot s_b
\]

块越小，scale 越贴合局部数值范围，精度越高、但 SF 占的存储越多。这是 OCP（Open Compute Project）MX 规范与 NVIDIA NVFP4 的核心思路。

### 2.2 两类 scale 编码

- **E8M0**（`float8_e8m0fnu`）：只有 8 位指数、没有尾数，即 scale 只能是 \(2^e\)。MXFP8 / MXFP4 / MXFP6 用它。
- **E4M3**（`float8_e4m3fn`）：1 符号 + 4 指数 + 3 尾数的普通 fp8，scale 可以是任意可表示值。NVFP4 用它。

### 2.3 cuBLAS/CUTLASS 的 128×4 blocked SF 布局

硬件加速要求 SF 不是简单的二维 `(行, K块)` 张量，而是被**重排（swizzle）**成 `(rm, rk, 32, 4, 4)` 的 6 维布局：每个内层 `(32, 4, 4)` 原子是 512 字节，覆盖「128 行 × 4 个 K 块」的一个 tile。QuACK、torchao 的 `to_blocked`、`torch._scaled_mm` 三者字节级一致。

> 关键术语：**SF（scale factor）**、**sf_vec_size（每个 SF 覆盖的逻辑元素数）**、**qdata（量化后的值）**、**收缩轴 / K 轴**。本讲承接 u4-l3（公共 GEMM API）的认知：GEMM 的 A/B 操作数除了普通张量，还可以是 `BlockScaledOperand`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/blockscaled/operand.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py) | `BlockScaledFormat` 格式描述符 + `BlockScaledOperand` 操作数容器，本讲主角 |
| [quack/blockscaled/quantize.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py) | 纯 PyTorch 量化器（`to_mx`/`to_mxfp4`/`to_nvfp4`…）与 128×4 blocked SF 重排 |
| [quack/blockscaled/utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/utils.py) | 测试/bench 用的操作数构造器、SM100 编译入口、dequant 工具的再导出 |
| [quack/blockscaled/__init__.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/__init__.py) | 子包公开导出（格式实例、量化器、容器类） |
| [quack/gemm_interface.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py) | 消费 `BlockScaledOperand` 的 GEMM 公共入口（解包与校验） |

---

## 4. 核心概念与源码讲解

### 4.1 BlockScaledFormat：格式的「唯一真相源」

#### 4.1.1 概念说明

一个块缩放格式由若干**正交属性**刻画：量化值用什么 dtype 存、单个元素多少 bit、每个 SF 覆盖多少元素、SF 用什么 dtype、是否需要 per-tensor scale……QuACK 把这些属性收进一个 **frozen dataclass** `BlockScaledFormat`，并立下铁律：**格式属性只能从这里读，任何下层代码都不得从张量 dtype 反推**。

为什么这么严格？因为反推会踩坑。最典型的陷阱：NVFP4 的 SF 是 E4M3，当它以 `uint8` 视图穿过自定义算子边界时，如果靠「uint8 ⇒ 推断成 e8m0」就会把它误当成 vec-32 的 MX 格式，静默算错。描述符把 dtype 显式记下来，就堵死了这类推断。

#### 4.1.2 核心流程

`BlockScaledFormat` 的字段构成格式的完整契约：

| 字段 | 含义 | 例（MXFP8_E4M3） |
|------|------|------------------|
| `name` | 格式名（跨 op schema 的唯一标识） | `"mxfp8_e4m3"` |
| `qdata_dtype` | 量化值的存储 dtype | `torch.float8_e4m3fn` |
| `cutlass_dtype_name` | CuTe-DSL MMA 元素类型名（可为 `None`） | `"Float8E4M3FN"` |
| `elem_bits` | 单个逻辑元素的位宽 | 8 |
| `elems_per_container` | 每个存储单元装几个逻辑元素 | 1 |
| `scale_dtype` | SF 的 dtype | `torch.float8_e8m0fnu` |
| `sf_vec_size` | 每个 SF 覆盖的逻辑 K 元素数 | 32 |
| `has_per_tensor_scale` | 是否支持 per-tensor scale | `False` |
| `storage_layout` | 存储布局标记（`None`/`container_v1`/`packed_lsb_v1`） | `None` |

其中 `storage_k(logical_k)` / `logical_k(storage_k)` 一对方法刻画「逻辑 K」与「存储 K」之间的映射——这是亚字节格式的关键：

- fp8：恒等，`storage_k(384) == 384`；
- fp4（两个元素装一字节）：`storage_k(384) == 192`；
- packed fp6（6-bit 小端比特流，4 个码装 3 字节）：`storage_k(384) == 288`。

#### 4.1.3 源码精读

格式描述符的定义与字段（注意它是 `frozen=True`，可哈希、可 pickle，能充当 dynamo guard 与内核缓存键）：

[quack/blockscaled/operand.py:65-92](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L65-L92) —— `BlockScaledFormat` 的字段声明，`sf_vec_size` 注释明确写了「== min logical-K divisibility」。

逻辑 K ↔ 存储 K 的映射，按 `storage_layout` 分两条路径：

[quack/blockscaled/operand.py:218-248](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L218-L248) —— `storage_k` / `logical_k`。`packed_lsb_v1` 走比特流除法，其余走 `elems_per_container` 整除。

四个代表性格式实例：

[quack/blockscaled/operand.py:281-299](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L281-L299) —— MXFP8_E4M3 / MXFP8_E5M2 / MXFP4 / NVFP4。注意 NVFP4 是唯一 `has_per_tensor_scale=True`、`scale_dtype=float8_e4m3fn`、`sf_vec_size=16` 的格式。

[quack/blockscaled/operand.py:318-327](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L318-L327) —— `MXFP6_E2M3_PACKED`，用 `storage_layout="packed_lsb_v1"` 标记 6-bit 紧凑比特流。

所有格式登记入册，`from_name` 用名字取回实例：

[quack/blockscaled/operand.py:356-369](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L356-L369) —— `BLOCKSCALED_FORMAT_REGISTRY`，格式的中央注册表。

硬件合法性判定 `mma_kind_for_pair` 决定一对 (A, B) 格式能映射到哪条 tcgen05 MMA 指令（`mxf4nvf4` 或 `mxf8f6f4`），它是「硬件能否表达」而非「是否已实现」：

[quack/blockscaled/operand.py:418-483](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L418-L483) —— `mma_kind_for_pair`。NVFP4 只能与自身配对（E4M3 scale + vec 16 是指令级配置），其余 fp8/fp6/fp4 在 e8m0/vec-32 下统一走 `mxf8f6f4`。

#### 4.1.4 代码实践

**目标**：用 `storage_k` / `logical_k` 验证三类格式的 K 映射，确认你对「逻辑 K vs 存储 K」的理解。

**操作步骤**（纯主机侧，无需 GPU）：

```python
# 示例代码：可在任意 Python REPL 运行（无需 CUDA）
from quack.blockscaled.operand import MXFP8_E4M3, MXFP4, NVFP4, MXFP6_E2M3_PACKED

for fmt in (MXFP8_E4M3, MXFP4, NVFP4, MXFP6_E2M3_PACKED):
    print(fmt.name, "is_packed=", fmt.is_packed,
          "storage_k(384)=", fmt.storage_k(384),
          "sf_vec_size=", fmt.sf_vec_size, "scale=", fmt.scale_dtype)
```

**需要观察的现象**：MXFP8 的 `storage_k(384)==384`（8-bit 恒等）；MXFP4 与 NVFP4 都是 `192`（4-bit 两元素一字节）；MXFP6 packed 是 `288`（6-bit，3/4 比例）。

**预期结果**：

```
mxfp8_e4m3 is_packed= False storage_k(384)= 384 sf_vec_size= 32 scale= torch.float8_e8m0fnu
mxfp4 is_packed= True storage_k(384)= 192 sf_vec_size= 32 scale= torch.float8_e8m0fnu
nvfp4 is_packed= True storage_k(384)= 192 sf_vec_size= 16 scale= torch.float8_e4m3fn
mxfp6_e2m3_packed is_packed= True storage_k(384)= 288 sf_vec_size= 32 scale= torch.float8_e8m0fnu
```

注意 NVFP4 的 `sf_vec_size=16` 与 `scale_dtype=float8_e4m3fn` 与其余三者都不同——这正是下一节要对比的重点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MXFP4` 的 `elems_per_container=2` 而 `MXFP6_E2M3_PACKED` 的 `elems_per_container=1`？

**答案**：`elems_per_container` 描述「传统容器布局」里一个存储单元装几个逻辑元素。MXFP4 用 `torch.float4_e2m1fn_x2`，一字节装两个 4-bit 元素，故为 2；MXFP6 packed 走 `packed_lsb_v1` 比特流路径，`elems_per_container` 对它无意义（置 1），真正的压缩比由 `storage_layout` + `elem_bits` 经比特除法算出（4 个 6-bit 码 = 24 bit = 3 字节）。

**练习 2**：调用 `MXFP6_E2M3_PACKED.storage_k(30)` 会发生什么？为什么？

**答案**：抛 `ValueError`（"does not fill whole storage elements"）。30 个 6-bit 码 = 180 bit，不能被 8（一个字节）整除，packed fp6 的行必须是整 3 字节组（即 K 必须被 4 整除）。

---

### 4.2 BlockScaledOperand：把权重值与 blocked SF 绑成一个容器

#### 4.2.1 概念说明

块缩放操作数有两个不可分离的部件：**qdata**（量化值）与 **scale**（128×4 blocked SF）。把二者用普通 `(data, scale_factor)` 元组传给 GEMM 有诸多问题：元组没有 dtype/shape、不能被 `torch.compile` 当作 pytree 叶子、对它做 `torch.mm` 会静默反量化或算错。QuACK 的解法是设计一个**不是 `torch.Tensor` 子类**的 frozen 容器 `BlockScaledOperand`：

- 显式、诚实的表面：`shape`/`dtype`/`mT`/`to`/`dequantize`；
- **没有 aten 拦截、没有反量化回退**：`torch.mm(t, t)` 直接 `TypeError`，逼你显式调用 `dequantize()`；
- 注册为 pytree 节点，能干净地穿过 `torch.compile` / functionalization 边界。

它的唯一消费者是 `quack.gemm*` 入口——「最简单的、能保持 (qdata, scale, format, pts) 原子的容器」就是它。

#### 4.2.2 核心流程

容器有 6 个字段：

```
qdata             # 量化值张量（存储真相）
scale             # 128×4 blocked SF（trailing (32,4,4) 原子，strides (16,4,1)）
format            # BlockScaledFormat 描述符
per_tensor_scale  # 仅 NVFP4：标量 fp32
orig_dtype        # 原始高精度 dtype（shape/dtype 报告逻辑视图）
quant_dim         # SF 沿哪条逻辑维分布（-1 或 -2）
```

构造期只校验**与模式无关的不变量**：qdata dtype 匹配格式、scale 是合法 128×4 原子、pts 合法、设备一致。**故意不校验 qdata-shape 与 scale-shape 的耦合**——varlen 操作数用填充过的 scale 缓冲，其形状依赖 `cu_seqlens`，耦合校验推迟到 GEMM dispatch。

`.mT`（转置）是 qdata 的**步长交换视图**，scale **原样携带**（SF 在两种朝向下都沿 K 分块，blocked 原子不可视图转置），`quant_dim` 在 -1/-2 间翻转。GEMM 接口里的 `_unpack_operand` 把容器拆成 `(qdata, scale, format, pts, quant_dim)` 五元组送入内核。

#### 4.2.3 源码精读

容器字段定义：

[quack/blockscaled/operand.py:550-576](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L550-L576) —— `BlockScaledOperand` 的字段。注意 `eq=False`：张量字段无法用 `==` 比较。

构造期校验，重点是 scale dtype 的规范化（uint8 视图会被 re-view 成格式 dtype，堵死「uint8 ⇒ e8m0」推断陷阱）：

[quack/blockscaled/operand.py:578-623](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L578-L623) —— `__post_init__`。`check_blocked_scale_atom` 校验 SF 原子；packed 格式会把 `quant_dim` 钉到 packed 维。

逻辑 `shape` 把存储 K 映射回逻辑 K（packed dim 还原）：

[quack/blockscaled/operand.py:627-636](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L627-L636) —— `shape` 属性。对 packed 格式用 `format.logical_k` 还原。

转置是步长交换视图，scale 不变：

[quack/blockscaled/operand.py:673-677](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L673-L677) —— `mT` 属性。

两条构造路径——`from_parts`（包装已量化的存储，用于 checkpoint/外部量化器）与 `quantize`（从高精度张量现量化）：

[quack/blockscaled/operand.py:696-716](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L696-L716) —— `from_parts`。

[quack/blockscaled/operand.py:718-783](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L718-L783) —— `quantize`。`dim=-2` 通过转置拷贝实现，返回 `quant_dim=-2` 视图，便于直接构造 (K,N) 的 B 操作数。

GEMM 接口的解包点，元组被显式拒绝：

[quack/gemm_interface.py:190-200](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L190-L200) —— `_unpack_operand`。`(data, scale_factor)` 元组抛 `TypeError`，并提示用 `from_parts` 包装。

#### 4.2.4 代码实践

**目标**：观察 `BlockScaledOperand` 如何把 qdata + blocked SF 绑成单一对象，并验证「转置只换 qdata 步长、scale 原样携带」。

**操作步骤**（需 CUDA；基于 `tests/test_blockscaled_operand.py` 的真实测试模式）：

```python
# 示例代码：在 GPU 机器上运行
import torch
from quack.blockscaled.operand import BlockScaledOperand, MXFP8_E4M3

torch.manual_seed(0)
x = torch.randn(256, 256, device="cuda", dtype=torch.bfloat16) * (256 ** -0.5)

t = BlockScaledOperand.quantize(x, MXFP8_E4M3)        # (M, K) 量化
print(repr(t))
print("scale.shape =", tuple(t.scale.shape), "scale.dtype =", t.scale.dtype)

tt = t.mT                                              # 转置视图
print("mT.scale is t.scale:", tt.scale is t.scale)     # scale 原样携带
print("mT.shape:", tt.shape, "mT.quant_dim:", tt.quant_dim)
```

**需要观察的现象**：`t.scale.shape` 形如 `(rm, rk, 32, 4, 4)`（对 256×256、vec=32：`rm=2, rk=2`）；`tt.scale is t.scale` 为 `True`（同一对象）；`mT.shape == (256, 256)` 且 `quant_dim` 由 `-1` 翻成 `-2`。

**预期结果**：

```
scale.shape = (2, 2, 32, 4, 4) scale.dtype = torch.float8_e8m0fnu
mT.scale is t.scale: True
mT.shape: (256, 256) mT.quant_dim: -2
```

（`repr` 的完整输出待本地验证，取决于设备与 torch 版本。）

#### 4.2.5 小练习与答案

**练习 1**：为什么对 `BlockScaledOperand` 调 `torch.mm(t, t.mT)` 会报 `TypeError` 而不是静默反量化？

**答案**：因为它**不是** `torch.Tensor` 子类，没有注册任何 aten 算子分发。容器只暴露显式方法（`dequantize`/`to`/`mT`），torch 算子在类型解析阶段就失败——这是「诚实表面」的设计：逼用户显式表达意图，避免在 packed 存储上算错。

**练习 2**：转置一个 fp4 操作数时，为什么不重新转置它的 scale？

**答案**：SF 的 blocked 原子 `(32,4,4)` 是硬件固定的 swizzle 布局，**不可视图转置**；且 SF 在两种朝向下都沿收缩轴（K）分块。所以 `.mT` 只交换 qdata 的步长，把 `quant_dim` 在 -1/-2 间翻转，scale 原样携带。

---

### 4.3 量化格式对比：MXFP8 / NVFP4 / MXFP4 / MXFP6

#### 4.3.1 概念说明

四类格式在两个维度上分化，这两个维度决定了精度、带宽与硬件指令路径：

1. **`sf_vec_size`（SF 向量长度）**：每个 SF 覆盖多少个逻辑元素。越小 → SF 越密 → 精度越高、SF 存储越大。
2. **`scale_dtype`**：E8M0（纯 2 的幂）vs E4M3（普通 fp8）。

| 格式 | 元素 | `elem_bits` | `sf_vec_size` | `scale_dtype` | per-tensor scale | 典型用途 |
|------|------|-------------|---------------|---------------|------------------|----------|
| MXFP8 | e4m3/e5m2 | 8 | **32** | E8M0 | 否 | 训练前向/激活/权重 |
| MXFP4 | e2m1 | 4 | 32 | E8M0 | 否 | 高压缩权重 |
| NVFP4 | e2m1 | 4 | **16** | **E4M3** | **是** | Blackwell 推理 |
| MXFP6 | e2m3/e3m2 | 6 | 32 | E8M0 | 否 | 精度/压缩折中 |

**MXFP8 vs NVFP4 是本讲实践要对比的核心**：MXFP8 用 32 元素一个 E8M0 scale，NVFP4 用 16 元素一个 E4M3 scale（再加一个可选 per-tensor fp32 scale）。同样 256 个 K 元素：MXFP8 产生 8 个 SF，NVFP4 产生 16 个 SF——NVFP4 的 SF 密度翻倍、且每个 SF 有 3 位尾数，故对 4-bit 极端低比特仍能保持精度。

#### 4.3.2 核心流程

所有量化器共享同一种「块归约取 max → 算 scale → 缩放后cast」骨架：

1. 把 K 轴 reshape 成 `(K // sf_vec_size, sf_vec_size)` 的块；
2. 每块取 `max_abs`；
3. 由 `max_abs` 算 biased scale（E8M0 的 rceil/floor，或 E4M3 的直接除法）；
4. `data_lp = data_hp / scale_fp32`，cast 到目标 dtype；
5. SF 从「二维 `(mn, sf_k)`」重排成「`(rm, rk, 32, 4, 4)` blocked 布局」。

E8M0 scale 的两种模式（MXFP8 独有）：
- **RCEIL**（默认）：\(e = \lceil \log_2(\max|x| / \mathrm{fp8\_max}) \rceil\)，保证块最大值永不饱和——NVIDIA MXFP8 预训练配方要求此模式以达 bf16 loss 对齐。
- **FLOOR**：torchao 默认，会裁剪 \((\mathrm{fp8\_max}, 2^{\mathrm{max\_pow2}+1})\cdot 2^e\) 区间的块最大值。

NVFP4 的 scale 是 E4M3：`block_scale = max_abs / F4_E2M1_MAX`，再乘以可选 per-tensor scale 的倒数。

#### 4.3.3 源码精读

MXFP8 量化器与 rceil/floor 分支：

[quack/blockscaled/quantize.py:47-108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L47-L108) —— `to_mx`。`sf_vec_size` 默认 32；scale 是 `float8_e8m0fnu`。

[quack/blockscaled/quantize.py:89-92](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L89-L92) —— rceil 与 floor 的分支选择。

NVFP4 量化器，`block_size` 必须 16、scale 是 E4M3、返回三元组（含 per-tensor scale）：

[quack/blockscaled/quantize.py:403-442](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L403-L442) —— `to_nvfp4`。

[quack/blockscaled/quantize.py:398-400](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L398-L400) —— `nvfp4_per_tensor_scale`：`amax / (F8E4M3_MAX * F4_E2M1_MAX) = amax / 2688`。

量化器注册表（eager fn, compiled fn）二元组，是「哪些格式能在仓库内量化」的唯一来源：

[quack/blockscaled/quantize.py:494-504](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L494-L504) —— `QUANTIZERS`。

128×4 blocked SF 重排，把 `(l, mn, sf_k)` 变成 `(l, rm, rk, 32, 4, 4)`：

[quack/blockscaled/quantize.py:637-662](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L637-L662) —— `pack_scale_2d_to_blocked_contig`。`rm = ceil(mn/128)`、`rk = ceil(sf_k/4)`，128 拆成 `(4 outer, 32 inner)` 再交换成 `(32, 4)`。

[quack/blockscaled/quantize.py:621-634](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L621-L634) —— `check_blocked_scale_atom`：校验 trailing 形状 `(32,4,4)` 与步长 `(16,4,1)`。

#### 4.3.4 代码实践

**目标**：对比 MXFP8 与 NVFP4 的 **SF 向量长度**，量化同一块数据，数 SF 个数。

**操作步骤**（纯 PyTorch 量化器，**CPU 可运行**——见 `tests/test_blockscaled_quantize.py` 顶部说明「CPU-runnable」）：

```python
# 示例代码：CPU 可运行
import torch
from quack.blockscaled.quantize import to_mx, to_nvfp4

torch.manual_seed(0)
x = torch.randn(4, 256, dtype=torch.bfloat16).contiguous()   # K=256

# MXFP8: sf_vec_size=32, scale 是 e8m0
q8, s8 = to_mx(x, block_size=32, scaling_mode="rceil")
# NVFP4: block_size=16, scale 是 e4m3, 返回三元组
q4, s4, pts = to_nvfp4(x, block_size=16, per_tensor_scale=None)

print("MXFP8 qdata:", q8.shape, q8.dtype, "| SF:", s8.shape, s8.dtype, "| SF数/行:", s8.shape[-1])
print("NVFP4 qdata:", q4.shape, q4.dtype, "| SF:", s4.shape, s4.dtype, "| SF数/行:", s4.shape[-1])
```

**需要观察的现象**：MXFP8 每行 256/32 = **8 个 SF**（E8M0）；NVFP4 每行 256/16 = **16 个 SF**（E4M3）。NVFP4 的 qdata 是 `uint8` 形状 `(4, 128)`（4-bit 两元素一字节），MXFP8 的 qdata 是 `(4, 256)` 的 fp8。

**预期结果**：

```
MXFP8 qdata: torch.Size([4, 256]) torch.float8_e4m3fn | SF: torch.Size([4, 8]) torch.float8_e8m0fnu | SF数/行: 8
NVFP4 qdata: torch.Size([4, 128]) torch.uint8 | SF: torch.Size([4, 16]) torch.float8_e4m3fn | SF数/行: 16
```

> 说明：`to_mx` / `to_nvfp4` 是**纯 PyTorch** 实现，CPU 即可运行（已在 `tests/test_blockscaled_quantize.py` 中以 CPU 验证）。但若要走 `to_mx_compiled` / `to_nvfp4_compiled`（torch.compile 生成的 Triton 内核）或 `BlockScaledOperand.quantize`（会调 compiled 路径并打包 SF），则需要 CUDA。完整结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：给定 K=128，MXFP8 和 NVFP4 各产生多少个 SF？为什么 NVFP4 用更密的 SF？

**答案**：MXFP8 产生 128/32 = 4 个 SF（E8M0）；NVFP4 产生 128/16 = 8 个 SF（E4M3）。NVFP4 是 4-bit 极端低比特，单个 E8M0（纯 2 的幂）scale 的粒度太粗会严重损失精度，因此用 2× 密度且带 3 位尾数的 E4M3 scale 来补偿。

**练习 2**：MXFP8 的 RCEIL 模式如何保证「块最大值永不饱和」？

**答案**：RCEIL 取 \(e = \lceil \log_2(\max|x|/\mathrm{fp8\_max}) \rceil\)，即最小的使 \(\max|x|/2^e \le \mathrm{fp8\_max}\) 的指数。因为 scale 是 2 的幂且向上取整，缩放后的块最大值必 ≤ fp8_max（448 或 57344），不会被 cast 裁剪。

---

### 4.4 量化工具层：quantize.py 与 utils.py

#### 4.4.1 概念说明

`quantize.py` 是从 torchao 移植的**纯 PyTorch 量化器**（避免运行时依赖 torchao），并提供 `torch.compile` 加速版（`to_mx_compiled` 等）。`utils.py` 则是**桥接层**：为测试/bench 构造完整的 blockscaled 操作数（`create_blockscaled_operand_quantized`）、提供 SM100 编译入口（`compile_blockscaled_gemm_tvm_ffi`）、再导出纯 torch 的反量化/打包工具。

关键设计：**量化数学与 SF 布局放在 `quantize.py`，使 `BlockScaledOperand.quantize/dequantize` 无需导入 CuTe-DSL 内核栈即可使用**。`utils.py` 因要构造 fake 张量与编译 GEMM，才依赖 cutlass/cute。

#### 4.4.2 核心流程

量化器统一返回 `(qdata, scale)`（NVFP4 多一个 pts）。`BlockScaledOperand.quantize` 在其上做四件事：

1. `_coerce_format` 把字符串/描述符规范化成 `BlockScaledFormat`；
2. `dim=-2` 时转置拷贝再递归（代价与 CUTLASS/torchao 的 dim-1 cast 相同）；
3. 调量化器（dynamo 下用 raw fn 以免嵌套编译，否则用 compiled fn）；
4. `pack_scale_2d_to_blocked_contig` 把二维 SF 重排成 blocked 布局。

`utils.py` 的 `BLOCKSCALED_FORMATS` 是「legacy 短名 → (qdata_dtype, scale_dtype, sf_vec_size)」的派生视图，**描述符才是真相源**；`create_blockscaled_operand_quantized` 是测试用的「bf16 randn → 量化 → 生成参考 + 操作数 + blocked scale」一站式构造器。

#### 4.4.3 源码精读

`BlockScaledOperand.quantize` 的核心，展示 qdata dtype 重视图 + SF 打包：

[quack/blockscaled/operand.py:762-783](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L762-L783) —— 量化器返回存储 dtype（fp4 为 uint8 nibble 对、packed fp6 为比特流、fp8 直接），按需 `.view(fmt.qdata_dtype)`，再 `pack_scale_2d_to_blocked_contig` 打包 SF。

`dequant_operand` 按 dtype 解码 qdata（fp4 一字节两码、packed fp6 先解包）：

[quack/blockscaled/quantize.py:593-618](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L593-L618) —— `dequant_operand`。`uint8` 字节对 packed-fp6 与 byte-container 有歧义，必须传 `BlockScaledFormat`。

`utils.py` 的 legacy 格式视图（描述符派生）：

[quack/blockscaled/utils.py:248-252](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/utils.py#L248-L252) —— `BLOCKSCALED_FORMATS`。

SM100 blockscaled GEMM 编译入口，接收**裸张量**（不是容器）：

[quack/blockscaled/utils.py:684-732](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/utils.py#L684-L732) —— `compile_blockscaled_gemm_tvm_ffi` 的签名与前置断言。注意它显式拒绝 `BlockScaledOperand`——解包是 `quack.gemm_interface` 的职责。

blockscaled GEMM 的参考数学（双 einsum，A、B 各乘自己的 SF）：

[quack/blockscaled/utils.py:957-967](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/utils.py#L957-L967) —— `blockscaled_gemm_reference`。

#### 4.4.4 代码实践

**目标**：跟踪一条「高精度张量 → 容器 → GEMM」的最小调用链，定位 SF 打包与解包点。

**操作步骤**（源码阅读型实践）：

1. 在 [quack/blockscaled/operand.py:780](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/operand.py#L780) 处找到 `pack_scale_2d_to_blocked_contig(sc.view(l, mn, k // fmt.sf_vec_size))`，确认 SF 数 = `k // sf_vec_size`。
2. 跟到 [quack/blockscaled/quantize.py:637](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize.py#L637) 的 `pack_scale_2d_to_blocked_contig`，确认输出形状 `(l, rm, rk, 32, 4, 4)`。
3. 跟到 [quack/gemm_interface.py:192](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L192) 的 `_unpack_operand`，确认容器在这里被拆成五元组送入内核。

**需要观察的现象**：SF 个数 `k // sf_vec_size` 对 MXFP8（vec=32）是 NVFP4（vec=16）的一半；打包后 SF 的 trailing 三维恒为 `(32, 4, 4)`、步长 `(16, 4, 1)`，与 cuBLAS `to_blocked` 一致。

**预期结果**：你能用一句话回答「容器如何把权重值与 blocked SF 绑定传给 GEMM」——`BlockScaledOperand` 持有 `qdata`（存储真相）与已 swizzle 成 `(rm,rk,32,4,4)` 的 `scale`，GEMM 接口的 `_unpack_operand` 把二者连同 `format`/`pts`/`quant_dim` 一起抽出送入 SM100 内核。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile_blockscaled_gemm_tvm_ffi` 显式拒绝接收 `BlockScaledOperand`？

**答案**：它是底层 TVM-FFI 编译路径，只认裸张量（raw qdata + scale 缓冲 + 显式 cutlass dtype）。容器的解包（抽取 qdata/scale/format/pts/quant_dim、校验、规范化）是上层 `quack.gemm_interface` 的职责；混在底层会破坏分层，所以用 `isinstance` 断言 + 提示「unwrap or call quack.gemm」拒绝。

**练习 2**：`dequant_operand` 为什么在收到 `uint8` 张量时要求必须传 `BlockScaledFormat`？

**答案**：`uint8` 字节对 packed-fp6（3 字节 4 码的比特流）和 byte-container（一字节一码）有歧义，单凭 dtype 无法判断该按 6-bit 解包还是直接读低 6 位。传格式后，函数看 `storage_layout == "packed_lsb_v1"` 决定是否先 `unpack_uint6`，再用 `_FLOATX_CODE_BITS` 表按 (ebits, mbits) 解码。

---

## 5. 综合实践

把本讲四个模块串起来：**从同一块 bf16 权重出发，分别量化成 MXFP8 与 NVFP4 操作数，对比 SF 密度与精度，再解释它如何作为容器传入 GEMM。**

1. 用 `to_mx`（CPU 可运行）把 `(4, 256)` bf16 量化成 MXFP8，记录 `qdata.shape`、`scale.shape`、SF 数/行。
2. 用 `to_nvfp4` 把同一块数据量化成 NVFP4，同样记录三项，并取回 `pts`。
3. 手算验证：MXFP8 的 SF 数 = 256/32 = 8，NVFP4 的 SF 数 = 256/16 = 16，正好 2× 关系。
4. （GPU 机器）用 `BlockScaledOperand.quantize(x, MXFP8_E4M3)` 与 `BlockScaledOperand.quantize(x, NVFP4)` 各构造一个容器，打印 `repr`，确认 `scale.shape` 的 trailing 是 `(32,4,4)`、`MXFP8.scale.dtype` 是 e8m0、`NVFP4.scale.dtype` 是 e4m3。
5. 写一句话解释：**为何把 qdata 与 blocked SF 绑进 `BlockScaledOperand` 而非传 `(data, sf)` 元组？**——参考 4.2 的「诚实表面、pytree 可序列化、单一真相源、元组被拒绝」四点。

**验收标准**：
- 能说出 MXFP8（vec 32 / E8M0）与 NVFP4（vec 16 / E4M3 + pts）在 `sf_vec_size` 与 `scale_dtype` 上的两点差异；
- 能解释 `.mT` 为何只换 qdata 步长、scale 原样携带；
- 能画出 `quantize → pack_scale_2d_to_blocked_contig → _unpack_operand` 的数据流。

> 步骤 1–3（纯量化器）CPU 可运行；步骤 4（容器 + compiled 量化）需 CUDA，完整数值结果待本地验证。

## 6. 本讲小结

- **块缩放量化**把 K 轴切成 `sf_vec_size` 大小的块，每块共用一个 SF，精度与 SF 存储开销由 `sf_vec_size` 与 `scale_dtype` 共同决定。
- **`BlockScaledFormat`** 是格式的「唯一真相源」frozen 描述符，`storage_k`/`logical_k` 处理亚字节格式的逻辑/存储 K 映射，任何下层都不得从 dtype 反推格式属性。
- **`BlockScaledOperand`** 是非 Tensor 的 frozen 容器，把 `qdata` + 已 swizzle 成 `(rm,rk,32,4,4)` 的 `scale` + `format`（+ NVFP4 的 `pts`）绑成一个原子单位；torch 算子对它直接报错，逼用户显式 `dequantize`。
- **MXFP8 vs NVFP4** 的核心差异：前者 vec=32 / E8M0 scale（RCEIL 保证不饱和），后者 vec=16 / E4M3 scale + 可选 per-tensor scale——SF 密度翻倍以补偿 4-bit 低精度。
- **`.mT`** 是 qdata 的步长交换视图、`scale` 原样携带、`quant_dim` 翻转；元组操作数被 GEMM 接口拒绝，必须用容器。
- **量化数学与 SF 打包**都在纯 PyTorch 的 `quantize.py`，使容器无需导入 CuTe-DSL 内核栈即可量化/反量化；`utils.py` 才是桥接编译/测试的 cutlass 依赖层。

## 7. 下一步学习建议

- **u7-l2（量化 GEMM 输出与 W4 权重）**：本讲只讲了量化「输入」操作数，下一篇讲量化「输出」（`BlockScaleFactorStore`、SFD/SFDCol）与 4-bit 权重的 W4 反量化路径。
- **u6-l5（领域 epilogue）**：回顾其中 `quantize_out` 一节，把本讲的 SF 生成与 epilogue 的 store 钩子连起来。
- **u5-l3（SM100 GEMM 与 TMEM）**：理解 `mma_kind_for_pair` 返回的 `mxf4nvf4` / `mxf8f6f4` 如何落到 tcgen05 MMA 指令，以及 2-CTA 模式对 blockscaled tile 几何的影响。
- 继续阅读源码：`tests/test_blockscaled_operand.py`（容器行为的完整断言）与 `tests/test_blockscaled_quantize.py`（CPU 可运行的量化数值测试）是验证你理解的最佳参考。
