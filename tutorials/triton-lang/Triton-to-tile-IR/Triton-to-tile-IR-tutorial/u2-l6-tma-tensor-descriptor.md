# TMA Tensor Descriptor 的拆解与启动

## 1. 本讲目标

本讲承接 u2-l5「Driver、Launcher 与内核启动」,聚焦一个 TileIR 后端独有的难题:**当 kernel 参数里出现 TMA tensor descriptor(张量描述符)时,它到底怎么被送进 GPU 内核**。

学完本讲,你应该能够:

1. 说清楚「host TMA」与「device TMA」的区别,以及为什么 CUDA Tile IR 只有 device 侧实现,从而迫使 Triton 在语言层把描述符「拆开」再传入内核。
2. 读懂 `make_tensordesc_arg` 把一个 `TensorDescriptor` 拆成 `[占位符, 数据指针, *shape, *strides]` 的逻辑,并对应到 C 侧的 `i32 / 指针 / i32 / i64` 类型序列。
3. 读懂 `TileIRLauncher` 在构建期对签名做 `expand_tensordesc` 展开、在调用期用 `wrap_handle_tensordesc` 对实参做同样拆解,最终把拆出的值喂给 `_launch` 的全过程。

---

## 2. 前置知识

### 2.1 什么是 TMA 与 tensor descriptor

TMA(Tensor Memory Accelerator)是 Hopper 及之后 GPU 的硬件单元,专门用来按「块(block)」搬运张量数据,而不是逐元素搬运。要用 TMA,硬件需要一份**张量描述符(tensor descriptor)**,它告诉 TMA:

- 数据从哪个基地址开始(`base` 指针);
- 张量的逻辑形状(`shape`);
- 每一维的步长(`strides`);
- 每次搬运多大的块(`block_shape`);
- 越界怎么填充(`padding`)。

在本仓库里,这份信息在 Python 侧封装成一个 dataclass:`triton.tools.tensor_descriptor.TensorDescriptor`。

### 2.2 host TMA vs device TMA

TMA 描述符有两种创建方式,这是理解本讲的**关键**:

| | host TMA | device TMA |
|---|---|---|
| 描述符在哪创建 | CPU(host)端 | GPU(device / kernel)端 |
| 上游 Triton(NVIDIA PTX 后端) | ✅ 支持 | ✅ 支持 |
| CUDA Tile IR 后端 | ❌ 不支持 | ✅ 支持 |
| 传给 kernel 的形式 | 一个不透明的 `CUtensorMap` 句柄(代码里叫 `nvTmaDesc`) | 把 base/shape/stride **拆开**逐个传,在内核内重建描述符 |

- 上游 NVIDIA PTX 后端走 host TMA:在 CPU 上用 `cuTensorMapEncode` 构造一个 `CUtensorMap`,把它当作一个不透明句柄直接传进内核,内核里 TMA 指令直接用这个句柄。
- CUDA Tile IR 后端**没有 host TMA 实现**。它只能拿到「裸」的 base 指针 / shape / stride,在内核内部由 device 侧 API 重建描述符。

> 一句话:PTX 后端把描述符「整块」传,TileIR 后端把描述符「拆碎」传。这正是本讲要讲的核心设计差异。

### 2.3 复习:u2-l5 的 launcher 结构

本讲会频繁用到 u2-l5 建立的认知:

- `TileIRLauncher` 在构建期调用 `make_launcher(constants, signature)`,根据 kernel 的参数签名现场拼出一段 C 胶水代码,即时编译成 `.so`。
- 这段胶水的入口是 `launch()`,它在解析完一个固定的「元数据前缀」(numTilesX/Y/Z、stream、function、launch_pdl、各种 hook 等)之后,才解析真正的 kernel 参数。
- 最终这些 kernel 参数被打包进 `void *params[]` 数组,交给 `cuLaunchKernelEx` 启动。

本讲要回答的,就是**当 kernel 参数里有 tensor descriptor 时,这个 `params` 数组里到底放了什么**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `third_party/tileir/backend/driver.py` | TileIR 后端启动器。本讲主角:`make_tensordesc_arg`、`expand_tensordesc`、`wrap_handle_tensordesc`、`TileIRLauncher`。 |
| `python/triton/language/semantic.py` | 语义层。`make_tensor_descriptor` 在 TileIR 下返回特殊的 `tileir_tensor_descriptor`;`_has_native_tma` 判定后端能力。 |
| `python/triton/language/core.py` | 描述符类型定义。`tileir_tensor_descriptor` / `tileir_tensor_descriptor_type` 比 base 版多一个 `ptr` 字段。 |
| `python/triton/tools/tensor_descriptor.py` | `TensorDescriptor` dataclass,即 host 侧描述符的数据载体。 |
| `python/triton/compiler/compiler.py` | `convert_type_repr` —— 上游 PTX 后端用 `nvTmaDesc` 标记描述符,是本讲用来做对比的依据。 |
| `README.md` | 第 84 行明确说明了 host TMA 降级到 device TMA 的设计动机。 |

---

## 4. 核心概念与源码讲解

### 4.1 host TMA 缺失设计:为什么必须在语言层拆描述符

#### 4.1.1 概念说明

本模块要回答一个「为什么」的问题:为什么 TileIR 后端不能像 PTX 后端那样,把描述符当句柄直接传?

答案的根因在于 **CUDA Tile IR 这一层 IR 本身只提供 device 侧 TMA API**。它的设计哲学是:由 CUDA Tile IR 编译器(即外部的 `tileiras`)在内部决定用 host 还是 device 方式实现 TMA;而在**语言层(Triton Python)只能看到 kernel 级的 API**。因此 Triton 原本那一套「在 host 上建好 `CUtensorMap` 再传句柄」的路径,在 TileIR 下根本没有落点。

于是 TileIR 的做法是:**在 Triton 语言层就把描述符降级(lowering)成它的组成部分**,即「基地址指针 + shape + stride」,让这些「原始材料」流过整个编译链路;真正需要描述符的地方(device 内核内),再由 device 侧的 `make_tensor_descriptor` 用这些材料现场重建。

> 这就是 README 里说的:「Support for lowering Triton host TMA APIs to CUDA Tile IR's TMA APIs」。它是一次「host TMA → device TMA」的语义降级。

#### 4.1.2 核心流程

把上面思路落到代码,流程如下:

1. **host 侧**用户调用 `TensorDescriptor.from_tensor(...)` 构造一份描述符对象(包含 base / shape / strides / block_shape)。
2. **kernel 内**调用 `tl.make_tensor_descriptor(base, shape, strides, block_shape, ...)`。语义层 `make_tensor_descriptor` 检测到 `target.backend == "tileir"` 时,**不**返回普通的 `tensor_descriptor`,而是返回一个多带 `base` 指针字段的 `tileir_tensor_descriptor`。这个 `base` 字段就是「device 侧重建描述符」要用的基地址,必须一路透传进内核。
3. **kernel 边界(启动)**:这份 `tileir_tensor_descriptor` 在签名里表现为 `tensordesc<dtype[block_shape]>` 这样的字符串。`TileIRLauncher` 把它展开成 `["i32", "*dtype", *(i32…), *(i64…)]`,实参侧把 `TensorDescriptor` 对象拆成 `[0, data_ptr, *shape, *strides]`,两侧一一对应。
4. **device 内核内**:内核拿到 ptr/shape/stride 后,由 device 侧 API 重建描述符并驱动 TMA。

#### 4.1.3 源码精读

README 第 84 行是这一设计的权威说明,明确点名了被修改的文件:

[README.md:L84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L84) —— 说明 CUDA TileIR 只有 device 实现,需要在 `core.py` / `semantic.py` / `tensor_descriptor.py` 把 host TMA API 降级。

语义层 `make_tensor_descriptor` 是这条降级链的起点。它先做一通通用校验,调用 builder 创建描述符 handle,然后在结尾根据后端分流:

[semantic.py:L1882-L1888](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1882-L1888) —— 创建描述符 handle 后,读 `target.backend`;若是 `"tileir"`,返回 `tl.tileir_tensor_descriptor(handle, shape, strides, type, base)`,**额外把 `base` 指针带上**;否则返回普通的 `tl.tensor_descriptor(...)`。

注意倒数第二行多传了一个 `base` 参数。这个 `base` 在普通 `tensor_descriptor` 里不存在,正是 TileIR 为了「device 侧重建」而必须透传的基地址。

另一个相关判定是 `_has_native_tma`,TileIR 在这里和 cuda 一样被视为「有原生 TMA」:

[semantic.py:L1098-L1100](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1098-L1100) —— 当 `target.backend` 是 `cuda` 或 `tileir` 且架构 ≥ 90(Hopper 及以上)时,认为后端有 native TMA。

#### 4.1.4 代码实践

**实践目标**:验证 TileIR 的 `make_tensor_descriptor` 返回类型与上游不同。

**操作步骤**:

1. 打开 `python/triton/language/semantic.py` 的 `make_tensor_descriptor`(约 L1845 起),找到末尾的 `if target.backend == "tileir":` 分支。
2. 对比同函数里上游返回的 `tl.tensor_descriptor(handle, shape, strides, type)` 与 TileIR 返回的 `tl.tileir_tensor_descriptor(handle, shape, strides, type, base)`。
3. 在 `python/triton/language/core.py` 里搜索 `class tileir_tensor_descriptor`,看它的 `__init__` 多接收并保存了哪个字段。

**需要观察的现象**:TileIR 版本的构造多了一个 `ptr` 字段,且其 `_flatten_ir` 会把 `self.ptr.handle` 一并放进扁平化的 IR 句柄列表里(见 4.2.3)。

**预期结果**:你能指出 TileIR 在 IR 层把描述符「展开」时,比上游多塞了一个指针类型的值。**待本地验证**:在真实 GPU 上用 `TRITON_KERNEL_DUMP=1` 抓到的 `.ttir` 里能否看到这个多出来的指针参数。

#### 4.1.5 小练习与答案

**Q1**:为什么 TileIR 不能复用上游 `make_tensor_descriptor` 直接返回的 `tensor_descriptor`?

**参考答案**:上游 `tensor_descriptor` 假设描述符句柄(host TMA 的 `CUtensorMap`)已经存在,只需要透传句柄即可。而 TileIR 没有 host TMA,内核内必须用「基地址指针」在 device 侧重建描述符,因此需要一种额外携带 `base` 指针的描述符类型,即 `tileir_tensor_descriptor`。

**Q2**:`_has_native_tma` 在 TileIR 下返回 `True`,是否意味着 TileIR 支持 host TMA?

**参考答案**:不是。`_has_native_tma` 只表示后端有「原生 TMA 能力」(device 侧),用来放行某些 dtype 检查(如 16 位浮点的 atomic min/max)。TileIR 的 TMA 是 device 侧的,与 host TMA 是两回事。

---

### 4.2 描述符拆解:`make_tensordesc_arg` 与签名展开

#### 4.2.1 概念说明

上一模块讲了「为什么拆」。本模块讲「怎么拆」。

拆解发生在**两个层面**,且两侧必须严格对齐:

- **类型层面(构建期)**:签名里的 `tensordesc<dtype[block_shape]>` 要被展开成一串 C 类型,这样 `make_launcher` 才能生成正确的 `PyArg_ParseTuple` 格式串和 `_launch` 参数列表。
- **数值层面(调用期)**:用户传入的 `TensorDescriptor` 对象要被拆成一串 Python 值,与上面那串 C 类型一一对应。

设描述符的秩(维度数)为 \(n\),dtype 为 \(\tau\),则两侧的对应关系是:

\[
\underbrace{\text{tensordesc}<\tau[\dots]>}_{\text{一个描述符}}
\;\Longrightarrow\;
[\,\text{i32},\ *\tau,\ \underbrace{\text{i32},\dots,\text{i32}}_{n\text{ 个 shape}},\ \underbrace{\text{i64},\dots,\text{i64}}_{n\text{ 个 stride}}\,]
\]

\[
\underbrace{\text{TensorDescriptor 对象}}_{\text{一份}}
\;\Longrightarrow\;
[\,0,\ \text{data\_ptr},\ s_0,\dots,s_{n-1},\ \sigma_0,\dots,\sigma_{n-1}\,]
\]

注意两个细节:

1. **第一个 `i32` 是占位符**。它的值永远是 `0`,作用是「占住描述符句柄在参数表里的位置」,但 TileIR 根本不用这个句柄(它在 device 侧重建)。源码注释直接点明:「nvidia oss backend passes tensordesc directly, but tileir needs to decompose it」。
2. **shape 用 `i32`,stride 用 `i64`**。这是因为 shape 是 int32、stride(按字节的步长)用 int64,与上游 `tensor_descriptor` 的约定一致。

#### 4.2.2 核心流程

调用期拆解的算法(`make_tensordesc_arg`)很简单:

```
输入: TensorDescriptor 对象 arg
1. data_ptr = arg.base.data_ptr()       # 取基地址(字节地址,整数)
2. shape    = arg.shape                 # list[int]
3. strides  = arg.strides               # list[int],且 strides[-1] == 1(只支持连续)
4. 返回 [0, data_ptr, *shape, *strides]
```

构建期签名展开的算法(`expand_tensordesc`)是对称的:

```
输入: 形如 "tensordesc<dtype[block_shape]>" 的字符串 value
1. shape = 解析 [...] 里的整数序列(其实是 block_shape)
2. dtype = 解析 < 与 [ 之间的类型名
3. 返回 ["i32", f"*{dtype}", *(["i32"] * len(shape)), *(["i64"] * len(shape))]
```

两者一对照:构建期有 `1 + 1 + n + n` 个类型,调用期有 `1 + 1 + n + n` 个值,完全对齐。

#### 4.2.3 源码精读

先看调用期的值拆解 `make_tensordesc_arg`:

[driver.py:L421-L431](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L421-L431) —— 从 `TensorDescriptor` 取出 `base.data_ptr()`、`shape`、`strides`,断言只支持连续张量(`strides[-1] == 1`),然后返回 `[0, data_ptr, *shape, *strides]`。注释明确说明 `0` 是替换 tensordesc 类型的占位符,并对比「nvidia oss backend 直接传 tensordesc,tileir 需要拆解」。

再看构建期的类型展开 `expand_tensordesc`(定义在 `TileIRLauncher.__init__` 内的闭包):

[driver.py:L482-L486](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L482-L486) —— 解析 `value` 字符串得到 `shape`(维度数由此决定)和 `dtype`,返回 `["i32", f"*{dtype}", *(["i32"] * len(shape)), *(["i64"] * len(shape))]`。

至于 `tileir_tensor_descriptor` 那个多出来的 `base` 指针字段在 IR 层是怎么体现的,看 `core.py` 的类型展开:

[core.py:L1531-L1539](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py#L1531-L1539) —— `tileir_tensor_descriptor_type._flatten_ir_types` 在「handle」之后、shape/stride 之前,显式插入了一个 `ptr_type`(指针类型)。代码注释写得很直白:「We need to insert ptr_type before shape and strides」。这就是 device 侧重建描述符所需基地址在 IR 里的位置。

对比一下上游 PTX 后端把描述符当不透明句柄的处理,这种「整块传」在 `compiler.py` 的 `convert_type_repr` 里一目了然:

[compiler.py:L39-L49](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L39-L49) —— 当类型 repr 里出现 `tt.nv_tma_desc = 1` 标记时,直接返回字符串 `'nvTmaDesc'`,即「把整个描述符当作一个不透明 TMA 描述符参数」。这正是 TileIR **不**走的路径。

> 小结:同样是「描述符参数」,PTX 后端在 IR 里是单个 `nvTmaDesc`,TileIR 后端在 IR 里是 `[handle占位, ptr, shape…, stride…]` 的一串。两者在 `convert_type_repr` 与 `expand_tensordesc` 处分道扬镳。

#### 4.2.4 代码实践

**实践目标**:用一个具体的二维描述符,手工算出「类型序列」和「值序列」,验证二者长度一致。

**操作步骤**:

1. 假设有一个 kernel 参数,其签名为 `tensordesc<fp32[64, 32]>`(即 dtype=fp32,block_shape=[64,32])。
2. 手动套用 `expand_tensordesc` 的逻辑,写出展开后的 C 类型列表。
3. 假设用户传入的 `TensorDescriptor` 的 `base.data_ptr() == 0x1000`、`shape == [128, 64]`、`strides == [64, 1]`,手动套用 `make_tensordesc_arg`,写出拆出的值列表。
4. 数一下两边各有几个元素。

**需要观察的现象**:类型列表与值列表元素个数相同,且逐位语义对应。

**预期结果**(可直接从源码推导,确定性结论):

- 类型序列(构建期):`["i32", "*fp32", "i32", "i32", "i64", "i64"]`,共 6 个。
- 值序列(调用期):`[0, 0x1000, 128, 64, 64, 1]`,共 6 个。

注意:这里的 shape/stride 取自 **`TensorDescriptor` 对象的逻辑 shape/stride**(如 `[128, 64]` / `[64, 1]`),而签名串 `tensordesc<fp32[64, 32]>` 里的 `[64, 32]` 是 **block_shape**(决定展开出多少个 i32/i64)。两者不一样:签名决定「拆出几个槽」,实参决定「每个槽填什么值」。

#### 4.2.5 小练习与答案

**Q1**:`make_tensordesc_arg` 返回列表的第一个元素为什么是 `0`?这个 `0` 最终去了哪里?

**参考答案**:它是描述符句柄槽的占位符。TileIR 不使用 host 侧描述符句柄(它在 device 侧重建描述符),所以这个位置填一个无意义的 `0` 占位,类型上对应一个 `i32`,被原样传进内核但内核不真正依赖它。

**Q2**:为什么 shape 用 `i32` 而 stride 用 `i64`?

**参考答案**:shape 维度大小用 32 位整数即可;而 stride 是按字节的步长,在大张量下可能超过 32 位范围,故用 64 位。这与上游 `tensor_descriptor` 的 shape/stride 类型约定保持一致。

**Q3**:如果 `strides[-1] != 1` 会怎样?

**参考答案**:`make_tensordesc_arg` 里有 `assert strides[-1] == 1`,会直接抛 `AssertionError`。当前 TileIR 只支持最末维连续(last-dim contiguous)的张量描述符。

---

### 4.3 launcher 参数展开:构建期签名 + 调用期实参

#### 4.3.1 概念说明

到目前为止我们知道「一个描述符」怎么拆。但真实 kernel 的参数列表里,描述符可能出现在普通位置,也可能**嵌套在元组里**(例如把一个描述符和一个标量打包成 tuple 传出)。本模块讲 `TileIRLauncher` 如何在两个时机统一处理这些情况:

- **构建期(`__init__`)**:扫描签名,只要有任何描述符(含嵌套在 tuple 里的),就把整张签名表「重写」成展开后的形态,再据此生成 C 胶水代码;同时记住「原始签名长度」`ori_signature_len`,用于处理某些 torch 版本少传 constexpr 参数的兼容问题。
- **调用期(`__call__` → `wrap_handle_tensordesc`)**:在真正启动前,把每个 kernel 实参里的 `TensorDescriptor` 对象拆成 `[0, data_ptr, *shape, *strides]`,元组里的描述符递归展开,然后把这些值「铺平」后追加到元数据前缀之后,交给已编译的 launcher。

> 关键认知:构建期改的是**类型串**(决定 C 代码长什么样),调用期改的是**值列表**(决定 params 数组里放什么)。两者用的拆解规则同构(`expand_tensordesc` ↔ `make_tensordesc_arg`,`expand_tuple` ↔ `_expand_tensordesc_tuple`)。

#### 4.3.2 核心流程

构建期(`TileIRLauncher.__init__`):

```
1. has_tensordesc = 签名里(含 tuple 嵌套)是否存在 tensordesc
2. 若存在,对每个签名项:
     - 是 tensordesc:  expand_tensordesc(value) → 展开成若干项,
                        每项以 key_tensordesc_idx 为新键写入 post_signature
     - 是 tuple:        expand_tuple(value) 递归展开内部描述符,保留 tuple 结构
     - 其它:            原样保留
3. self.signature = post_signature
4. make_launcher(constants, self.signature) 生成 C 胶水并编译
5. 用 wrap_handle_tensordesc 包一层,得到 self.launch
```

调用期(`wrap_handle_tensordesc` 的 `inner`):

```
1. args = [元数据前缀...] + [kernel 实参...]
2. meta_args        = args[:9]          # 固定元数据前缀
3. raw_kernel_args  = args[9:]          # 真正的 kernel 参数
4. 对每个 raw_kernel_args 里的 arg:
     - TensorDescriptor: final_args.extend(make_tensordesc_arg(arg))   # 铺平
     - tuple:            final_args.append(_expand_tensordesc_tuple(arg))  # 递归,保留 tuple
     - 其它:             final_args.append(arg)
5. 调用 launcher(*meta_args, *final_args)
```

> 关于「元数据前缀长度为 9」:这是 `make_launcher` 生成的胶水所约定的固定前缀(numTilesX/Y/Z、stream、function 等),其精确含义已在 u2-l5 讲过,本讲把它当作一个不透明前缀即可。注意 `TileIRLauncher.__call__` 在调用前会把 `launch_pdl` 插入前缀,所以传到 `inner` 的前缀长度正是 9。

#### 4.3.3 源码精读

先看构建期的签名重写,这是 `TileIRLauncher.__init__` 的核心。它用三个闭包 `is_tensordesc` / `contains_tensordesc` / `expand_tensordesc` / `expand_tuple` 来处理「直接描述符」和「嵌套在 tuple 里的描述符」两种情况:

[driver.py:L499-L527](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L499-L527) —— 判断 `has_tensordesc`;若为真,把签名逐项重写:直接描述符用 `expand_tensordesc` 展开(新键形如 `{key}_tensordesc_{idx}`),tuple 用 `expand_tuple` 就地展开内部描述符而保留 tuple 结构;最后据此生成 launcher,并用 `wrap_handle_tensordesc` 包一层。注释说明「嵌套描述符就地展开,使生成的 launcher 保留原始 tuple 结构」。

> 旁注:`_tensordesc_{idx}` 这种新键名只是给 C 侧生成 `_argN` 占位用的,语义上仍属于原来的那一个 kernel 参数。

再看调用期的实参拆解。`wrap_handle_tensordesc` 返回的 `inner` 就是真正替换 `self.launch` 的函数:

[driver.py:L446-L461](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L446-L461) —— 把 args 切成元数据前缀和 kernel 实参;逐个处理实参:`TensorDescriptor` 用 `make_tensordesc_arg` 铺平,`tuple` 用 `_expand_tensordesc_tuple` 递归,其它原样保留;最后拼回调用。

其中处理嵌套 tuple 的递归函数:

[driver.py:L434-L443](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L434-L443) —— `_expand_tensordesc_tuple`:对 tuple 里每个元素,若为 `TensorDescriptor` 则 `extend` 铺平,若为 tuple 则递归并 `append`(保留嵌套),否则原样 `append`。

> 时间线:这套「描述符嵌套在 tuple 里」的支持是较新加入的,对应提交 `e232e0109 [TileIR] handle tensor descriptors nested in tuples`。在那之前,`has_tensordesc` 用的是 `any("tensordesc" in value ...)` 这种粗略判断,且不支持嵌套;现在改成 `contains_tensordesc` 递归判断 + `expand_tuple` 递归展开。

最后,拆出来的值如何进入 C 胶水。`make_launcher` 在生成 C 代码时,对每种类型决定「如何从 Python 对象取值」(即 `internal_args_list`):

[driver.py:L160-L170](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L160-L170) —— 对每个签名类型决定传给 `_launch` 的实参形式:指针类型(`*xxx`)走 `ptr_info{i}.dev_ptr`(由 `getPointer` 从 Python 对象提取设备指针);`nvTmaDesc` 走 `*tma_ptr{i}`(解引用);浮点走打包后的 `_storage`;普通整型走 `_arg{i}`。

对我们的描述符展开序列 `["i32", "*fp32", "i32", "i32", "i64", "i64"]` 而言:

- 第 1 个 `i32`(占位符 `0`)→ `_arg` = 0;
- `*fp32` → 走 `getPointer` 提取 `dev_ptr`;
- 后面的 `i32` / `i64` → 直接作为整型 `_arg` 传入。

这正是「拆出的值」最终汇入 `_launch` 的 `void *params[]` 数组、再交给 `cuLaunchKernelEx` 的路径。

#### 4.3.4 代码实践(本讲主实践)

**实践目标**:完整追踪一个 `tensordesc` 参数,从签名字符串 `expand_tensordesc` 展开,到调用期 `make_tensordesc_arg` 拆值,再到 `_launch` 的 `params[]`,列出拆出的 i32 / 指针 / shape / stride 序列。这是本讲规格里指定的实践任务。

**操作步骤**(源码阅读 + 手工跟踪,无需 GPU):

1. 设 kernel 签名(单个 kernel 参数)为:`a: tensordesc<fp32[64, 32]>`,key 记作 `(0,)`(第一个参数)。
2. **构建期** —— 套用 [driver.py:L482-L486](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L482-L486) 的 `expand_tensordesc`,写出 `post_signature` 的新键与类型。
3. **调用期** —— 设用户传入的 `TensorDescriptor` 满足 `base.data_ptr()==0x7f000000`、`shape=[256,128]`、`strides=[128,1]`,套用 [driver.py:L421-L431](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L421-L431) 的 `make_tensordesc_arg`,写出拆出的值列表。
4. **C 侧** —— 对照 [driver.py:L160-L170](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L160-L170),把每个值标注成 `_launch` 里对应的来源(`_arg` / `dev_ptr` / `_arg` …),并写出最终进入 `void *params[]` 的顺序。
5. **进阶**:把 kernel 参数改成嵌套形式 `a: (tensordesc<fp32[64,32]>, i32)`,重做第 2、3 步,验证 tuple 结构被保留、内部描述符仍被铺平(用 `expand_tuple` / `_expand_tensordesc_tuple`)。

**需要观察的现象**:构建期类型与调用期值一一对应;嵌套 tuple 时,「tuple 这一层」被保留,但 tuple 内部的描述符被铺平。

**预期结果**(单描述符情形,可直接从源码推导):

| 位置 | post_signature 键 | C 类型 | 拆出的值 | `_launch` 来源 |
|---|---|---|---|---|
| 0 | `(0,)_tensordesc_0` | `i32` | `0` | `_arg`(占位符) |
| 1 | `(0,)_tensordesc_1` | `*fp32` | `0x7f000000` | `ptr_info.dev_ptr`(getPointer 提取) |
| 2 | `(0,)_tensordesc_2` | `i32` | `256` | `_arg`(shape 第 0 维) |
| 3 | `(0,)_tensordesc_3` | `i32` | `128` | `_arg`(shape 第 1 维) |
| 4 | `(0,)_tensordesc_4` | `i64` | `128` | `_arg`(stride 第 0 维) |
| 5 | `(0,)_tensordesc_5` | `i64` | `1` | `_arg`(stride 第 1 维) |

这 6 个值就按上表顺序进入 `void *params[]`,随 `cuLaunchKernelEx` 启动内核。**待本地验证**:在 Blackwell + CUDA 13.1 上,用 dump 工具确认 kernel 入口的参数布局与本表一致。

> 嵌套 tuple 情形(步骤 5)的结论:签名侧 `expand_tuple` 返回 `(["i32","*fp32","i32","i32","i64","i64"], "i32")`(外层仍是 tuple);实参侧 `_expand_tensordesc_tuple` 返回 `((0, 0x7f000000, 256, 128, 128, 1), 那个 i32 值)`。即「tuple 容器保留、内部描述符铺平」,与构建期对称。

#### 4.3.5 小练习与答案

**Q1**:`TileIRLauncher` 为什么要在 `__init__` 里记录 `self.ori_signature_len`?

**参考答案**:用于 `__call__` 里的兼容逻辑——某些 torch(inductor)版本在启动时不会把 constexpr 参数传进 launch 函数。通过比较实参数与 `ori_signature_len`,可从 `self.constants` 里补回缺失的 constexpr 参数。这与描述符拆解无直接关系,但共享同一段 `__call__` 代码。

**Q2**:如果 kernel 没有任何描述符参数,`self.launch` 是什么?还会经过 `wrap_handle_tensordesc` 吗?

**参考答案**:`has_tensordesc` 为假时,`self.signature` 直接用原签名,`self.launch = mod.launch`,**不**经过 `wrap_handle_tensordesc` 包装。包装只在有描述符时才发生(见 [driver.py:L523-L526](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L523-L526))。

**Q3**:为什么「构建期用 `expand_tensordesc`、调用期用 `make_tensordesc_arg`」是两套函数,而不是一套?

**参考答案**:两者作用对象不同——构建期处理的是**类型字符串**(签名里的 `tensordesc<...>`),输出 C 类型列表;调用期处理的是**运行时对象**(`TensorDescriptor` 实例),输出具体数值列表。虽然它们的「形状」同构(都是 `1+1+n+n`),但一个面向类型、一个面向值,故分开实现。

---

## 5. 综合实践

把本讲三个模块串起来,完成一次「端到端」的描述符参数追踪。

**任务**:给定如下最小 kernel 形态(仅为说明,非项目原有代码,标注为示例代码):

```python
# 示例代码:仅用于说明参数流,非仓库内文件
@triton.jit
def copy_kernel(a, b, M, N):
    desc = tl.make_tensor_descriptor(a, shape=[M, N], strides=[N, 1], block_shape=[64, 32])
    block = desc.load([0, 0])
    # ... 使用 block ...
```

要求:

1. **降级点**:指出 `tl.make_tensor_descriptor` 在 TileIR 下走 [semantic.py:L1886-L1887](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1886-L1887) 的哪个分支,返回类型多了什么字段。
2. **IR 层**:对照 [core.py:L1531-L1539](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py#L1531-L1539),说明这个多出的字段在扁平化 IR 类型里的插入位置。
3. **启动签名**:假设 `a` 在签名里是 `tensordesc<fp32[64, 32]>`,写出构建期展开后的类型序列。
4. **启动实参**:假设 host 侧 `a = TensorDescriptor.from_tensor(tensor, block_shape=[64,32])`,写出 `make_tensordesc_arg(a)` 的结果(用符号表示 `data_ptr`)。
5. **对比**:用一句话说明,如果是 PTX 后端,`a` 会在 [compiler.py:L43-L45](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L43-L45) 处被映射成什么,从而体现两条后端的本质差异。

**预期产出**:一份包含上述 5 点答案的简短笔记,能清楚说明「TileIR 在语言层把 host TMA 降级为 device TMA,因此描述符被拆成 占位符+指针+shape+stride 传入内核」。

---

## 6. 本讲小结

- **根因**:CUDA Tile IR 只有 device 侧 TMA,没有 host TMA;Triton 因此在语言层把 host TMA API 降级成「base 指针 + shape + stride」,在内核内由 device API 重建描述符。
- **语义层降级**:`make_tensor_descriptor` 在 `target.backend == "tileir"` 时返回多带 `base` 指针的 `tileir_tensor_descriptor`;该指针在 IR 扁平化时被插在 handle 与 shape/stride 之间。
- **拆解规则**:一个描述符(秩 \(n\))被拆成 `[占位 i32, 指针, *(i32 shape), *(i64 stride)]`,共 \(2+2n\) 个槽;构建期 `expand_tensordesc` 决定类型,调用期 `make_tensordesc_arg` 决定数值。
- **两个时机**:`TileIRLauncher.__init__` 在构建期重写签名并生成 C 胶水;`wrap_handle_tensordesc` 在调用期把 `TensorDescriptor` 实参铺平,两侧规则同构。
- **嵌套支持**:描述符可嵌套在 tuple 里,由 `expand_tuple` / `_expand_tensordesc_tuple` 递归处理(对应提交 `e232e0109`),「tuple 容器保留、内部描述符铺平」。
- **后端对比**:PTX 后端把描述符当不透明 `nvTmaDesc` 整块传(`convert_type_repr`),TileIR 后端拆碎传入——这是两条后端在 TMA 上的本质差异。

---

## 7. 下一步学习建议

- **向上回看**:若对「kernel 内描述符如何被 device API 使用」感兴趣,可继续读 `semantic.py` 中 `descriptor_load` / `descriptor_store` / `descriptor_atomic_*`(约 L1065-L1140),它们是描述符在 device 侧的实际访存入口。
- **横向对比**:读 `python/triton/backends/driver.py` 里的 `decompose_descriptor` / `expand_signature` / `make_tensordesc_args`,理解上游 PTX 后端那套「不透明 `nvTmaDesc`」的拆解机制,与本讲的 TileIR 拆解做对照。
- **向后衔接**:本讲讲的是「启动期参数如何展开」。下一篇 u2-l7「tileiras 外部编译器调用与 cubin 生成」会讲这些参数对应的内核 cubin 是怎么由外部 `tileiras` 编译出来的,从而把整条编译/启动链路闭环。
