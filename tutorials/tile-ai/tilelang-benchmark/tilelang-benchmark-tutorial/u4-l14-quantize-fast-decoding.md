# 量化与 lop3 快速解码

## 1. 本讲目标

本讲聚焦一个看似不起眼、却直接决定量化算子性能的环节：**把压缩存放的权重「解包」回可计算的数值**。

读完本讲，你应当能够：

1. 说清 W4A16 场景下「比特打包」与「解包」的对应关系，能算出 `num_elems_per_byte`、压缩后的 `B_shape`、以及第 `j` 个 nibble 在字节里的位置。
2. 读懂标量解包 `_tir_packed_int_to_int_convert` 的调用约定，理解它为何是一条「移位 + 掩码」的朴素路径。
3. 理解 lop3 快速解码为什么能比标量解包快——核心是 PTX `lop3` 指令能用一条指令完成任意三输入布尔函数。
4. 掌握 TileLang 把外部 C 函数注入内核的三件套：`T.import_source` 注入源码、`T.call_extern` 调用、`T.address_of` 传指针。
5. 看懂 `T.dp4a`（INT8 四路点积）在内核里的位置，并理解 `use_dp4a` 是如何根据 dtype 自动选择点积引擎的。

本讲承接 [u4-l13 反量化 GEMV 内核：线程级外积规约](u4-l13-dequant-gemv-thread-reduction.md)。上一讲讲的是「线程如何分工、如何 allreduce」；本讲钻进那条解包语句，回答「一个被压成 4 bit 的权重，到底是怎么变回一个能参与乘加的数值的」。

## 2. 前置知识

### 2.1 量化与 W4A16 回顾

在 [u4-l12](u4-l12-int8-multiprecision-gemm.md) 我们讲过混合精度的「窄输入、宽累加」原则。量化的极端版本是把权重压到 4 bit（甚至 2 bit），称为 **W4A16**：权重 W 用 4 bit 存（Weight-4bit），激活 A 仍是 16 bit 浮点（Activation-16bit）。

为什么压到 4 bit？因为大语言模型的推理几乎总是**带宽受限**（尤其是 decode 阶段 M=1 的 GEMV）。把权重从 fp16（16 bit）压到 int4（4 bit），访存直接减少 4 倍，带宽瓶颈随之缓解。代价是：算之前必须先把 4 bit 解包回 fp16/int8，这一步本身就是开销——本讲的主角就是「让这一步尽量便宜」。

### 2.2 比特打包（bit packing）

4 bit 没法按字节直接寻址（一个字节 8 bit），所以存放时会把**两个 4 bit 权重塞进一个 8 bit 字节**里。这就是「打包」：

```
字节内容（8 bit）:  [ 高 4 bit ][ 低 4 bit ]
                     value_1      value_0
```

读出来时，要取出第 `j` 个值，就是「右移 `j*4` 位、再掩码低 4 位」：

\[
\text{val}_j = (b \gg (j \cdot \text{num\_bits})) \ \& \ ((1 \ll \text{num\_bits}) - 1)
\]

其中 `num_bits=4`，`(1<<4)-1 = 0b1111 = 15`，正是 4 位掩码。

### 2.3 lop3 指令背景（先建立直觉）

标量解包的问题是：**每取出一个值都要「移位 + 掩码」两条指令**，一个字节里的两个 nibble 就要 4 条指令，扩展到整个寄存器（32 bit = 8 个 nibble）就更多。

现代 NVIDIA GPU（Volta 之后）有一条 PTX 指令 `lop3`，它能用**一条指令**计算「任意三输入的布尔函数」：

\[
\text{result} = f(a, b, c)
\]

`f` 由一个 8 位立即数（lookup table，LUT）指定，LUT 的 8 个 bit 恰好对应 `(a,b,c)` 三位二进制的 8 种组合的输出。换句话说，一条 `lop3` 能等价于一组 `AND/OR/XOR/NOT` 组合。

lo p3 快速解码的精髓是：用一串精心挑选的 `lop3`（配合少量移位），**一次性把多个 nibble 之间插入 0**，让 8 个紧挨着的 4 bit 值变成 8 个独立的 8 bit 值，从而大幅减少指令条数。这正是 Marlin、exllama 等高性能 INT4 推理内核的共同技巧。

> 说明：本讲只讲清 `lop3` 的原理与 TileLang 的接驳方式。具体的 C 源码字符串由 `tilelang.quantize` 在运行时生成（见 4.3），其内部实现属于 tilelang 包，不在本仓库内，相关取值需在本地打印确认。

## 3. 本讲源码地图

本讲只精读一个文件，但它是整条「压缩→解包→点积」链条的缩影：

| 文件 | 作用 |
| --- | --- |
| [benchmark_tilelang_matmul_fp16xint4.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py) | TileLang 的 W4A16 反量化 GEMV 内核。内含标量解包、lop3 快速解码、dp4a 点积三条互斥分支，由 `fast_decoding` 与 dtype 共同选择走哪一条。 |

辅助参考（仅作对照，不在本讲深入）：

- `benchmark_tilelang_matmul_fp16xfp4.py`：同目录的 2 bit（fp4）变体，用一个**手写的标量解包函数** `_tir_u8_to_u2_to_u8`，可对比「自己写解包」与「调用 `tilelang.quantize`」两种风格。
- `benchmark_tilelang_matmul.sh`：按 `(m,n,k)` 形状循环驱动该内核并写日志。

## 4. 核心概念与源码讲解

### 4.1 比特打包关系：storage_dtype / num_bits / num_elems_per_byte

#### 4.1.1 概念说明

这个模块回答三个数：**权重用什么容器存（`storage_dtype`）、每个权重占几位（`num_bits`）、一个容器能塞几个权重（`num_elems_per_byte`）**。这三个数决定了权重张量 `B` 的最终形状，也决定了每次访存要取多少字节。

本文件默认配置（见 `main()`）：

- `storage_dtype = "int8"`：用 8 bit 字节当容器。
- `num_bits = 4`：每个权重 4 bit。
- `in_dtype = "float16"`：解包后权重还原成 fp16 参与计算。

#### 4.1.2 核心流程

从「容器位宽」与「单值位宽」推出「每个容器装几个值」：

\[
\text{num\_elems\_per\_byte} = \frac{\text{storage\_nbit}}{\text{num\_bits}}
\]

默认下 `num_elems_per_byte = 8 // 4 = 2`，即每个 int8 字节装 2 个 int4 权重。

于是权重张量的「逻辑 K 维」被压缩一半：

\[
K_{\text{stored}} = \frac{K}{\text{num\_elems\_per\_byte}} = \frac{K \cdot \text{num\_bits}}{\text{storage\_nbit}} = \frac{K}{2}
\]

这正是 `B_shape = (N, K // storage_nbit * num_bits)` 的来历（注意写法：`K // storage_nbit * num_bits`，先除后乘，等价于 `K // num_elems_per_byte`）。

#### 4.1.3 源码精读

[benchmark_tilelang_matmul_fp16xint4.py:36-43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L36-L43) 把容器类型拆成「字母」和「位数」两半，再算出每个容器装几个值、以及每次访存取多少元素：

```python
storage_type = "".join(c for c in storage_dtype if not c.isdigit())  # "int8" -> "int"
storage_nbit = int("".join(c for c in storage_dtype if c.isdigit()))  # "int8" -> 8
num_elems_per_byte = storage_nbit // num_bits                          # 8 // 4 = 2

MAX_TRANSACTION_SIZE_IN_BITS = 128
micro_size_k = MAX_TRANSACTION_SIZE_IN_BITS // DataType(in_dtype).bits        # 128//16 = 8
micro_size_k_compressed = micro_size_k // num_elems_per_byte                  # 8 // 2 = 4
block_K = reduce_thread * micro_size_k                                         # 32*8 = 256
```

- `storage_type`/`storage_nbit` 是把字符串 `"int8"` 拆成 `"int"` 与 `8`，后续 `_tir_packed_int_to_int_convert` 要用 `("int", 8)` 标识字节类型。
- `micro_size_k`：单次访存按 **128 bit（一条 128 位事务）** 切，fp16 每个 16 bit，故一次取 8 个元素。
- `micro_size_k_compressed`：压缩后每 2 个元素共用一字节，故一次只取 4 字节（仍 128 bit 对齐）。

紧接着 [L48-L50](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L48-L50) 给出三个张量的形状，注意 `B` 的第二维是**压缩后**的：

```python
A_shape = (M, K)
B_shape = (N, K // storage_nbit * num_bits)   # 压缩一半
C_shape = (M, N)
```

#### 4.1.4 代码实践

1. **目标**：把「压缩关系」手算一遍，验证 `B` 的形状确实是逻辑 K 的一半。
2. **步骤**：
   - 取默认参数 `K=8192, num_bits=4, storage_nbit=8`。
   - 手算 `num_elems_per_byte`、`K_stored`、`micro_size_k`、`micro_size_k_compressed`、`block_K`（取 `reduce_thread=32`）。
3. **预期结果**：

   | 量 | 值 |
   | --- | --- |
   | num_elems_per_byte | 2 |
   | K_stored（B 第二维） | 4096 |
   | micro_size_k | 8 |
   | micro_size_k_compressed | 4 |
   | block_K | 256 |

4. **延伸**：把 `num_bits` 想象成 2（fp4 场景），重算 `num_elems_per_byte`（=4）、`K_stored`（=2048），体会「位数越低、压缩越狠」。

#### 4.1.5 小练习与答案

**Q1**：若把 `storage_dtype` 改成 `"int32"`、`num_bits` 仍为 4，一个容器装几个权重？  
**答**：`storage_nbit=32`，`num_elems_per_byte = 32//4 = 8`，一个 int32 装 8 个 int4。

**Q2**：`B_shape` 表达式写成 `K // storage_nbit * num_bits` 而非 `K * num_bits // storage_nbit`，两者数值上等价吗？为什么作者这样写？  
**答**：在 `K` 是 `storage_nbit//num_bits` 的整数倍时两者相等。作者写成「先除后乘」是为了让结果落在「容器个数」这一语义上，直观表达「压缩后的存储长度」。

---

### 4.2 标量解包：_tir_packed_int_to_int_convert

#### 4.2.1 概念说明

`_tir_packed_int_to_int_convert` 来自 `tilelang.quantize`（[import 见 L6-L7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L6-L7)），它是「朴素的逐元素解包」工具——对每个压缩字节，按位置取出对应的 nibble，再转成目标 dtype。

注意它是**柯里化（curry）**调用：先传 `(storage_type, storage_nbit)` 得到一个「转换器」，再用 `(num_bits, byte, pos, dtype)` 调用该转换器。这种两段式让你可以复用同一个转换器处理很多字节。

它对应 2.2 节的朴素公式，逐个 nibble 串行处理，逻辑直观但指令多——这就是 4.3 节 lop3 要优化的对象。

#### 4.2.2 核心流程

标量解包位于 `fast_decoding=False` 的 `else` 分支，对 `micro_size_k` 个待解包元素**逐个**循环：

```
for ki in [0, micro_size_k):
    # ki // num_elems_per_byte : 这个值落在第几个压缩字节里
    # ki % num_elems_per_byte  : 它在该字节里是第几个 nibble（位置 pos）
    B_dequantize_local[ki] = convert(num_bits, B_quant_local[字节号], pos, in_dtype)
```

关键映射：**第 `ki` 个解包值 ↔ 第 `ki // 2` 个字节里的第 `ki % 2` 个 nibble**。

#### 4.2.3 源码精读

[benchmark_tilelang_matmul_fp16xint4.py:120-125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L120-L125) 是标量解包路径：

```python
else:
    for ki in T.serial(micro_size_k):
        B_dequantize_local[ki] = _tir_packed_int_to_int_convert(
            storage_type, storage_nbit)(num_bits, B_quant_local[ki // num_elems_per_byte],
                                        ki % num_elems_per_byte, in_dtype)
```

对照阅读 [L91-L92](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L91-L92) 的两个 buffer，理解「压缩输入 / 解包输出」的尺寸差：

```python
B_quant_local = T.alloc_local([micro_size_k_compressed], storage_dtype)  # 4 字节（压缩）
B_dequantize_local = T.alloc_local([micro_size_k], in_dtype)             # 8 个值（解包后）
```

——4 个字节解出 8 个值，正是 `num_elems_per_byte=2`。

> 对照参考：同目录 `benchmark_tilelang_matmul_fp16xfp4.py` 没有用 `tilelang.quantize`，而是自己写了一个 `_tir_u8_to_u2_to_u8`（见该文件 L16-L21），本质也是 `(val >> (pos*nbit)) & ((1<<nbit)-1)`。两相对比，能看出 `_tir_packed_int_to_int_convert` 是把这个模式通用化、参数化了。

#### 4.2.4 代码实践

1. **目标**：手推标量解包对一个具体字节的输出。
2. **步骤**：取一个压缩字节 `b = 0b1000_0011 = 131`，`num_bits=4`，`num_elems_per_byte=2`。
   - `pos=0`：`(131 >> 0) & 0xF = 0b0011 = 3`。
   - `pos=1`：`(131 >> 4) & 0xF = 0b1000 = 8`。
3. **预期结果**：一个字节解出两个值 `[3, 8]`（低 nibble 在前）。
4. **延伸思考**：注意 `source_format="uint"`（见 [L19](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L19)）表示这里把 nibble 当**无符号整数**直接还原；若是「对称量化」还需乘 scale、减 zero_point，本内核默认 `with_scaling=False` 不做后处理。

#### 4.2.5 小练习与答案

**Q1**：`_tir_packed_int_to_int_convert` 为什么用两段调用（先 `(storage_type, storage_nbit)`，再 `(num_bits, byte, pos, dtype)`）？  
**答**：第一段绑定「容器的存储类型」，第二段绑定「单次取值的参数」。两段分开后，同一个转换器可在循环里被多次复用，编译期也便于按 `(storage_type, storage_nbit)` 特化代码。

**Q2**：标量解包循环用的是 `T.serial` 而非 `T.vectorized`，为什么这里没有向量化？  
**答**：因为取值要先按 `ki // num_elems_per_byte` 定位字节、再按 `ki % num_elems_per_byte` 定位 nibble，相邻 `ki` 访问的是**同一个字节的不同部分**，存在共享依赖，难以直接做规则的向量化加载；而加载 `B_quant_local` 那一步（[L106-L111](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L106-L111)）才用 `T.vectorized`。

---

### 4.3 lop3 快速解码：get_lop3_intrin_group + import_source + call_extern

#### 4.3.1 概念说明

这是本讲的核心。标量解包「逐 nibble 移位掩码」，指令条数随元素数线性增长；**lop3 快速解码**改用一串 PTX `lop3` 指令，把多个 nibble 并行展开为字节，指令数大幅下降。

在 TileLang 里，这套 lop3 解码逻辑不是手写在内核里的，而是由 `tilelang.quantize.get_lop3_intrin_group` **在运行时生成一段 C 源码**和对应的函数名，再通过三件套注入内核：

| 原语 | 作用 |
| --- | --- |
| `get_lop3_intrin_group(...)` | 根据位宽/格式/存储类型，返回 `{"c_source": <C 代码字符串>, "func_name": <函数名>}` |
| `T.import_source(c_source)` | 把那段 C 源码注入到当前内核的编译单元，使其可被链接调用 |
| `T.call_extern(func_name, args..., dtype=...)` | 在内核里发出一条「调用外部函数」的指令，按名字调用上面注入的 C 函数 |
| `T.address_of(buf[0])` | 取缓冲区首地址，把指针传给 C 函数 |

这是一种 **host 侧生成代码 + device 侧调用** 的模式：Python 端拼好 C 源码，编译期注入，运行期被 kernel 当普通函数调用。

#### 4.3.2 核心流程

`fast_decoding` 分支的流程：

```
（Python 端，构造内核时）
1. get_lop3_intrin_group(out_dtype, source_format, source_bit,
                        storage_dtype, with_scaling, with_zeros)
   -> 拿到 c_source 字符串 与 func_name
2. 在 @T.prim_func 里：T.import_source(c_source)   # 注入 C 源

（Device 端，每次 K 循环迭代）
3. T.call_extern(func_name,
                 T.address_of(B_quant_local[0]),       # 入参：压缩字节首地址
                 T.address_of(B_dequantize_local[0]),  # 出参：解包结果首地址
                 dtype=in_dtype)
```

注意分工：**函数名和源码是 Python 期动态决定的**，所以无法从本仓库静态读出 `func_name` 的确切字符串——它由 tilelang 包的 intrin 注册表决定（「待本地验证」，见 4.3.4）。

#### 4.3.3 源码精读

先看 [L55-L74](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L55-L74)，Python 端在构造内核时按需取 lop3 信息：

```python
import_source: Optional[str] = None
func_name: str = ""
if fast_decoding is True:
    # Lazy import to decrease the startup time
    # as intrin registry may take a while to load
    from tilelang.quantize import get_lop3_intrin_group

    lop3_intrin_info = get_lop3_intrin_group(
        out_dtype=in_dtype,
        source_format=source_format,   # "uint"
        source_bit=num_bits,            # 4
        storage_dtype=storage_dtype,    # "int8"
        with_scaling=with_scaling,      # False
        with_zeros=False,
    )
    import_source = lop3_intrin_info["c_source"]
    func_name = lop3_intrin_info["func_name"]
```

两个细节值得留意：
- **惰性导入**：注释明说「intrin registry 加载较慢」，所以把 `from tilelang.quantize import get_lop3_intrin_group` 放进 `if fast_decoding` 分支里，不开快速解码就不付这个启动代价。
- **入参语义**：`out_dtype=in_dtype`（解包后的目标类型）、`source_format`（`"uint"` 表示按无符号整数还原）、`source_bit=num_bits`（原始位宽）、`storage_dtype`（容器类型）。这些参数决定了生成哪一段 C 代码。

注入发生在内核内部 [L99](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L99)：

```python
T.import_source(import_source)
```

最后是 device 端调用，[L113-L119](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L113-L119)：

```python
if fast_decoding:
    T.call_extern(
        func_name,
        T.address_of(B_quant_local[0]),
        T.address_of(B_dequantize_local[0]),
        dtype=in_dtype,
    )
```

读这段要抓三个要点：
1. **替换了整段 `for ki in T.serial(...)` 循环**——原本逐元素解包的循环被一次外部函数调用取代，这正是「快速」的来源。
2. **传的是两个指针 + dtype**：源（压缩）、目的（解包）缓冲首地址。被调函数内部对整段缓冲做 lop3 解包，比标量循环快。
3. **`dtype=in_dtype` 是「缓冲类型标注」**，告诉编译器这次外部调用的数据类型，便于类型检查与代码生成。

#### 4.3.4 代码实践（本讲主任务）

1. **目标**：对比 `fast_decoding=True/False` 两条解包分支，写出 lop3 注入的函数名取值来源与 `call_extern` 的实参清单。
2. **步骤**：
   - 在 [L62](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L62) 之后插入一行临时打印：`print("LOP3 func_name:", func_name); print("LOP3 c_source:\n", import_source)`。
   - 分别把 `main()` 里的 `fast_decoding` 设为 `True`（已是默认）和 `False`，运行 `python benchmark_tilelang_matmul_fp16xint4.py --m 1 --n 1024 --k 8192`（需要 tilelang 环境）。
3. **需要观察的现象**：
   - `fast_decoding=True` 时，打印出 lop3 的函数名与 C 源码字符串；`fast_decoding=False` 时该打印不触发（`import_source` 为 `None`，`func_name` 为空串）。
4. **预期结果（静态可确定部分）**：
   - **`call_extern` 的实参**（无论函数名取何值都固定为这四个）：`func_name`、`T.address_of(B_quant_local[0])`（压缩缓冲首地址，入参）、`T.address_of(B_dequantize_local[0])`（解包缓冲首地址，出参）、`dtype=in_dtype`（类型标注，默认 `"float16"`）。
   - **lop3 注入的 C 函数名**：即 `lop3_intrin_info["func_name"]` 的运行时取值，由 `get_lop3_intrin_group` 依据 `(out_dtype="float16", source_format="uint", source_bit=4, storage_dtype="int8")` 在 intrin 注册表里查得。**确切字符串待本地验证**（典型的 lop3 解包 intrinsic 命名形如 `__lop3_*` 系列，但请以本地打印为准，勿臆断）。
5. **若无法运行**：明确标注「待本地验证」。即便不运行，也应能从源码静态回答「函数名从哪个变量来、call_extern 传了哪几个实参」。

#### 4.3.5 小练习与答案

**Q1**：为什么把 `from tilelang.quantize import get_lop3_intrin_group` 放在 `if fast_decoding is True:` 里，而不是文件顶部？  
**答**：注释写了「intrin registry 加载较慢」。放在分支内是**惰性导入**，只有开启快速解码才加载注册表，关闭时不付启动开销。

**Q2**：`T.import_source` 与 `T.call_extern` 是什么关系？少其中一个会怎样？  
**答**：`T.import_source` 负责「把 C 源码塞进编译单元、让函数被定义并链接进来」；`T.call_extern` 负责「在内核里调用这个已定义的函数」。只有 `call_extern` 没 `import_source`，链接器找不到该函数符号；只有 `import_source` 没 `call_extern`，源码被编译进去却无人调用，等于白注入。

**Q3**：标量解包（4.2）和 lop3 解包（4.3）在数值上应该等价吗？  
**答**：应该等价。两者都是「把同样的压缩字节解成同样的 nibble 序列」，只是实现路径不同（移位/掩码 vs lop3 指令序列）。`fast_decoding` 是**纯性能开关**，不改语义。

---

### 4.4 点积引擎：T.dp4a 与 use_dp4a 分派

#### 4.4.1 概念说明

解包之后要做点积。这里有两条点积路径：

- **标量乘加**：`accum += a * b`，一个元素一个元素地算。适合 fp16 输入。
- **`T.dp4a`**：调用 GPU 的 `__dp4a` 内置函数，**一次算 4 个 INT8 元素的点积、累加到一个 INT32**。适合 int8 输入（即 W4A8 场景）。

`__dp4a(a[4], b[4], c)` 的语义：

\[
c \leftarrow c + \sum_{i=0}^{3} a_i \cdot b_i
\]

一条指令完成 4 路 INT8 乘加，是 INT8 Tensor Core 之外的标量 INT8 加速指令。

#### 4.4.2 核心流程

内核用一个布尔变量 `use_dp4a` 在编译期选择路径：

```
use_dp4a = (in_dtype == "int8") and (accum_dtype == "int32")

if use_dp4a:
    for ki in [0, micro_size_k) step 4:    # 每 4 个一组
        T.dp4a(A_local[ki], B_dequantize_local[ki], accum_res[0])
else:
    for ki in [0, micro_size_k):
        accum_res[0] += A_local[ki] * B_dequantize_local[ki]
```

> 重要事实：本文件默认 `in_dtype="float16"`、`accum_dtype="float16"`（见 [L159-L161](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L159-L161)），所以 `use_dp4a=False`，**默认走的是标量乘加路径，`T.dp4a` 并不会被触发**。`T.dp4a` 分支是为 W4A8（int8 激活 + int32 累加）准备的，需要改 dtype 才会激活。

#### 4.4.3 源码精读

判定逻辑在 [L52-L53](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L52-L53)：

```python
dp4a_size = 4
use_dp4a = in_dtype == "int8" and accum_dtype == "int32"
```

两条互斥的点积分支在 [L127-L136](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L127-L136)：

```python
if use_dp4a:
    for ki in T.serial(micro_size_k // dp4a_size):
        T.dp4a(
            A_local[ki * dp4a_size],
            B_dequantize_local[ki * dp4a_size],
            accum_res[0],
        )
else:
    for ki in T.serial(micro_size_k):
        accum_res[0] += A_local[ki] * B_dequantize_local[ki]
```

读这段要抓两点：
- `T.dp4a` 接收的是**每组的起始元素**（`ki*4`），它内部会向后取连续 4 个元素做点积；所以循环步长是 `micro_size_k // 4`。
- `accum_res[0]` 是「读改写」累加器，`T.dp4a` 把点积结果累加进去。

无论走哪条，结果都写进 `accum_res[0]`，随后交给 [L138-L151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L138-L151) 的 `T.tvm_thread_allreduce` 做跨线程归约（这一步在 [u4-l13](u4-l13-dequant-gemv-thread-reduction.md) 已讲透，本讲不重复）。

#### 4.4.4 代码实践

1. **目标**：理解 `use_dp4a` 是纯 dtype 驱动的，不靠任何额外开关。
2. **步骤**：在源码里找三处——
   - `in_dtype`/`accum_dtype` 的默认值（[L159-L161](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L159-L161)）。
   - `use_dp4a` 的判定（[L53](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L53)）。
   - 两条点积分支（[L127-L136](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L127-L136)）。
3. **需要观察的现象**：默认 dtype 下哪条分支生效？
4. **预期结果**：默认走 `else`（标量 fp16 乘加）。若想让 `T.dp4a` 生效，需把 `in_dtype="int8"`、`accum_dtype="int32"`（即切到 W4A8 配置）——此时解包出的权重会被当 int8 与 int8 激活做四路点积。
5. **延伸思考（待本地验证）**：理论上同一形状下 W4A8（dp4a）应比 W4A16（标量 fp16 MAC）算力更高，但 W4A16 访存更省；到底谁快取决于该形状是算力受限还是带宽受限。

#### 4.4.5 小练习与答案

**Q1**：`T.dp4a` 为什么循环步长是 `micro_size_k // dp4a_size` 而非 `micro_size_k`？  
**答**：因为 `__dp4a` 一次处理连续 4 个元素，所以循环按 4 个一组推进，循环次数自然是元素数除以 4。

**Q2**：默认配置下 `T.dp4a` 会被调用吗？为什么？  
**答**：不会。默认 `in_dtype="float16"`、`accum_dtype="float16"`，`use_dp4a = ("float16"=="int8") and ...` 为 `False`，走标量乘加分支。`dp4a` 仅在 W4A8（int8/int32）时激活。

**Q3**：把一个内核同时写得能跑 W4A16 和 W4A8，代价是什么？  
**答**：代价是内核里同时保留两条点积分支、一个 `use_dp4a` 判定，代码更长；好处是**结构与精度解耦**——换 dtype 即换路径，调度骨架（线程分工、allreduce、解包缓冲）完全复用。这正是 DSL 相对硬编码 kernel 的价值（呼应 [u4-l12](u4-l12-int8-multiprecision-gemm.md)）。

## 5. 综合实践

把本讲的「压缩 → 解包 → 点积」三段串起来，做一次「全链路静态走查」。

**任务**：给定默认参数 `num_bits=4, storage_dtype="int8", in_dtype="float16", accum_dtype="float16", reduce_thread=32, n_partition=4, fast_decoding=True`，回答：

1. **压缩关系**：`num_elems_per_byte`、`micro_size_k`、`micro_size_k_compressed`、`block_K` 各是多少？（用 [4.1](#41-比特打包关系storage_dtype--num_bits--num_elems_per_byte) 的公式）
2. **解包分支**：本配置走标量解包还是 lop3？函数 `dequantize_gemv` 内部，`import_source` 与 `func_name` 是怎么被赋值的？写出 [L99](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L99) 与 [L113-L119](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L113-L119) 各自的作用。
3. **点积分支**：本配置走 `T.dp4a` 还是标量乘加？为什么？要切换到 dp4a 需要改哪两个变量？
4. **进阶（可选，需 tilelang 环境）**：按 [4.3.4](#434-代码实践本讲主任务) 插入打印，分别跑 `fast_decoding=True/False`，记录 lop3 的 `func_name` 与 `c_source` 长度，并对比两次的 latency（同 shape），体会「快速解码」到底快多少。若无法运行，标注「待本地验证」。

**参考答案要点**：
1. `num_elems_per_byte=2`，`micro_size_k=8`，`micro_size_k_compressed=4`，`block_K=256`。
2. `fast_decoding=True` 走 lop3。`import_source`/`func_name` 由 `get_lop3_intrin_group(...)` 返回的字典取出（[L62-L71](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L62-L71)）；`T.import_source` 把 C 源注入编译单元；`T.call_extern` 用 `func_name` 调用它，传两个指针 + dtype。
3. 走标量乘加（`use_dp4a=False`，因 dtype 为 fp16）。要切 dp4a 需把 `in_dtype="int8"`、`accum_dtype="int32"`。

## 6. 本讲小结

- **压缩三要素**：`storage_dtype`（容器）、`num_bits`（单值位宽）、`num_elems_per_byte = storage_nbit // num_bits`（每容器装几个值），它们决定 `B` 的压缩形状与每次访存的字节数。
- **标量解包** `_tir_packed_int_to_int_convert`：两段式柯里化调用，逐 nibble 做「移位 + 掩码」，朴素但指令多。
- **lop3 快速解码**：用 PTX `lop3`（一条指令完成任意三输入布尔函数）并行展开 nibble，比标量解包指令数大幅减少；在 TileLang 里由 `get_lop3_intrin_group` 动态生成 C 源，经 `T.import_source` 注入、`T.call_extern` 调用、`T.address_of` 传指针。
- **三件套分工**：`import_source` 定义、`call_extern` 调用、`address_of` 传址，缺一不可；`func_name` 是运行时取值，确切字符串待本地验证。
- **点积分派**：`use_dp4a = (in_dtype=="int8" and accum_dtype=="int32")` 是纯 dtype 驱动；`T.dp4a` 一次算 4 个 INT8 点积累加到 INT32；**本文件默认 fp16 配置走标量乘加，dp4a 仅在 W4A8 激活**。
- **以代码为准**：`fast_decoding` 是性能开关不改语义；标量与 lop3 数值等价；同内核复用 W4A16/W4A8 两套精度路径，体现「结构与精度解耦」的 DSL 价值。

## 7. 下一步学习建议

- 继续往量化基线生态走：[u4-l15 fp4/int4 反量化 matmul 与量化基线生态](u4-l15-fp4-int4-and-quant-baselines.md) 会把 int4（本讲的 uint 解包）与 fp4 对比，并串联 Marlin、CUTLASS fpa_intb、bitsandbytes nf4 等同类基线——你会发现 Marlin 正是以 lop3 解码闻名的框架，可与本讲互相印证。
- 若想看「块级 GEMM 而非线程级 GEMV」如何处理量化，回顾 [u3-l9 块级 GEMM 内核解剖](u3-l9-block-gemm-anatomy.md) 与同目录 `benchmark_tilelang_matmul_fp16xfp4.py`（它用块级 `T.gemm` + 手写 `_tir_u8_to_u2_to_u8` 解包，是另一种解包风格）。
- 若想深入 lop3 的指令级原理，建议在具备 tilelang 环境的机器上按 [4.3.4](#434-代码实践本讲主任务) 打印 `c_source`，对照 PTX `lop3.b32` 的 LUT 语义阅读那段 C 代码——这是把「快速解码」从概念落到指令的最后一公里。
