# torch op 分派：从 Python 调用到 C++ schema

## 1. 本讲目标

本讲是「一个算子从 Python 到 CUDA」链路的第二站。上一篇（u2-l1）讲了 `import sgl_kernel` 时如何根据 GPU 架构加载对应的 `.so` 扩展；本讲要回答加载完成后的下一个问题：

> Python 里一行 `torch.ops.sgl_kernel.rmsnorm.default(...)` 是怎么找到并调用到 C++ 里那个真正干活的 `rmsnorm` 函数的？

学完后你应当能够：

1. 读懂 sgl-kernel 里 `m.def("...")` 这一行 schema 字符串，说出每个参数的类型、是否可变、返回什么。
2. 区分 `m.def`（声明算子契约）与 `m.impl`（绑定设备实现）这两步，并理解为什么大多数算子要绑定到 `torch::kCUDA`。
3. 理解 `Tensor!`（可变张量）与 `-> ()`（无返回）这对组合的含义，以及它为何是高性能推理算子的主流写法。
4. 看懂 Python 包装层「校验 + 预分配输出 + 转发到 `torch.ops.sgl_kernel.<name>.default`」的标准套路，并能据此定位任意算子。

本讲只讲「分派机制」本身，不深入 CUDA kernel 内部实现（那是 u2-l3 的主题）。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么是 PyTorch Custom Op

PyTorch 内置算子（如 `torch.add`）和一个普通 Python 函数最大的区别是：普通函数「调用即执行」，而 PyTorch 算子要先经过一个叫 **Dispatcher（分派器）** 的中间层。Dispatcher 根据输入张量的**设备**（CPU/CUDA）、**dtype**、是否需要**自动求导**等条件，决定真正该跑哪一段代码。

sgl-kernel 的 CUDA 算子要享受 Dispatcher 的能力（尤其是 `torch.compile` 图捕获、CUDA graph 兼容），就必须把自己**注册**成一个 PyTorch Custom Op。注册完成后，它就和 `torch.add` 一样，能通过 `torch.ops.<命名空间>.<算子名>` 被调用。

### 2.2 契约与实现分离：schema 的直觉

注册一个 Custom Op 分两步，可以类比「写接口」和「写实现」：

- **第一步 `m.def`**：用一段字符串声明这个算子叫什么、吃哪些参数、每个参数什么类型、哪些参数会被改写、返回什么。这段字符串叫 **schema**，它是算子的**契约**。`torch.compile` 和 Dispatcher 只看 schema，不看实现。
- **第二步 `m.impl`**：把一个真正干活的 C++ 函数，绑定到**某个设备**（例如 CUDA）上，作为这个算子在该设备上的实现。

这种分离带来的好处是：同一个算子名可以为 CPU、CUDA、ROCm 各绑一份实现，而上层 Python 代码完全不用改。

### 2.3 「原地写回」为什么是高性能推理的常态

在训练框架里，函数通常「输入只读、返回新张量」。但在推理算子里你会大量看到相反的写法：**算子不返回任何东西（`-> ()`），而是把结果直接写进一个调用者预先分配好的输出张量（`Tensor!`）**。原因有三个，本讲会在源码里逐一印证：

1. **避免每次调用都分配显存**——分配/释放是昂贵的。
2. **输出地址稳定**——这是 CUDA graph 捕获的前提（地址变了，图就失效）。
3. **便于显存复用**——同一个 buffer 可以被反复改写。

带着这三个直觉，进入源码。

## 3. 本讲源码地图

本讲涉及四个文件，它们正好构成「注册 → 声明 → 包装 → 导出」的完整一环：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `csrc/common_extension.cc` | CUDA 后端的算子注册中心 | `TORCH_LIBRARY_FRAGMENT`、`m.def`/`m.impl`、`REGISTER_EXTENSION` |
| `include/sgl_kernel_ops.h` | C++ 函数声明头文件 | schema 里的类型如何对应到真实 C++ 签名 |
| `python/sgl_kernel/elementwise.py` | elementwise 类算子的 Python 包装 | rmsnorm/silu_and_mul 的校验、预分配、转发 |
| `python/sgl_kernel/moe.py` | MoE 类算子的 Python 包装 | moe_align_block_size/topk_softmax 的转发 |

另外会顺带引用 `python/sgl_kernel/__init__.py`（导出层）和 `README.md`（官方贡献说明）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：注册机制（4.1）、schema 语法与可变语义（4.2）、Python 包装转发（4.3）。

### 4.1 TORCH_LIBRARY_FRAGMENT 与 m.def/m.impl 双步注册

#### 4.1.1 概念说明

要把一批 C++ 函数变成 PyTorch 算子，需要把它们登记到一个**命名空间**里。sgl-kernel 用的命名空间就叫 `sgl_kernel`，所以 Python 端调用时写的是 `torch.ops.sgl_kernel.<算子名>`。

登记动作由两个宏/方法完成：

- **`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m) { ... }`**：打开一个「库片段」作用域，`m` 是一个库对象（`torch::Library`），在这个大括号里可以连续注册很多算子。用 `FRAGMENT`（片段）而非 `TORCH_LIBRARY` 的原因是：**片段可以被多个文件重复打开**，向同一个命名空间里追加算子。sgl-kernel 有多个扩展（common/flash/spatial/infllm…），它们各自向 `sgl_kernel` 命名空间贡献算子，必须用片段形式。
- **`m.def` / `m.impl`**：分别完成「声明契约」和「绑定实现」。

#### 4.1.2 核心流程

注册阶段的执行流程（编译期/加载期发生一次）：

```text
TORCH_LIBRARY_FRAGMENT(sgl_kernel, m) {
    for 每个算子:
        m.def("<算子名>(<参数 schema>) -> <返回>")   // 1. 声明契约
        m.impl("<算子名>", torch::kCUDA, &cpp_func)   // 2. 绑定 CUDA 实现
}
REGISTER_EXTENSION(common_ops)                        // 3. 生成 Python 模块入口
```

调用阶段（运行期，每次 `torch.ops.sgl_kernel.xxx(...)`）：

```text
Python 调用
   └─> Dispatcher 查表：算子名 + 输入张量的设备
         └─> 设备是 CUDA ─> 跳到 m.impl 绑定的 &cpp_func
              └─> 执行 CUDA kernel
```

注意注册与调用方向相反：注册时是「先契约后实现」，调用时是「按契约查实现」。

#### 4.1.3 源码精读

整个注册中心的开头和结尾：

- [csrc/common_extension.cc:21-21](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L21-L21)：`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m) {` 打开片段作用域，后续所有 `m.def/m.impl` 都在这个作用域内，向 `sgl_kernel` 命名空间注册。
- [csrc/common_extension.cc:484-484](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L484-L484)：`REGISTER_EXTENSION(common_ops)`。这个宏（定义在头文件里）展开后生成 `PyInit_common_ops`，即 Python `import` 这个 `.so` 时的模块入口。没有它，`.so` 就不是一个合法的 Python 扩展模块。

注册一个算子有两种写法。第一种是**一行式**（schema 字符串与 catch-all 实现合并）：

- [csrc/common_extension.cc:25-25](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L25-L25)：`m.def("get_graph_buffer_ipc_meta", &get_graph_buffer_ipc_meta);`——把函数指针直接传给 `m.def`，PyTorch 自动从 C++ 签名推导 schema，并注册一个**不限设备**（catch-all）的实现。适合不涉及张量设备分派的纯工具函数。

第二种是**两步式**（显式 schema 字符串 + 设备绑定），这也是 sgl-kernel 里绝大多数 CUDA 算子用的形式：

- [csrc/common_extension.cc:31-34](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L31-L34)：`init_custom_ar` 先用 `m.def("init_custom_ar(...) -> int")` 写死 schema，再用 `m.impl("init_custom_ar", torch::kCUDA, &init_custom_ar)` 把实现**绑定到 CUDA 设备**。

为什么 CUDA 算子普遍用两步式？README 在「Development Tips」里写得很直白：

> // We need def with schema here for torch.compile

也就是说，**显式 schema 字符串是 `torch.compile` 正确工作的前提**——dynamo 追踪时需要一段稳定、可解析的算子签名，而不依赖 C++ 模板的自动推导。这一点是 sgl-kernel 选择两步式的核心动机。

`m.impl` 的设备绑定分两种，源码里都能看到：

- **绑定到具体设备**：`m.impl("rmsnorm", torch::kCUDA, &rmsnorm)`——只在输入是 CUDA 张量时分派到这个实现（绝大多数算子）。
- **catch-all（不绑设备）**：[csrc/common_extension.cc:407-407](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L407-L407) 的 `m.impl("apply_token_bitmask_inplace_cuda", &ApplyTokenBitmaskInplace);` 没有第二个 `torch::kCUDA` 参数，是一个跨设备的 Composite 实现。

> 术语：catch-all 实现在 PyTorch 里通常对应 CompositeImplicitAutograd 等分发键，表示「这段实现不依赖具体后端，任何设备都能用」。本讲只需记住「没有设备参数 = 全设备通用」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手在注册中心走一遍注册流程。

1. **实践目标**：分清「一行式」与「两步式」，并说出各自的使用场景。
2. **操作步骤**：
   - 打开 `csrc/common_extension.cc`，通读第 21–50 行（allreduce 段）。
   - 找出第 25–29 行里所有「一行式」的 `m.def(..., &fn)`。
   - 找出第 31–39 行里「两步式」的 `m.def` + `m.impl`。
   - 数一数：整个文件里 `m.impl` 第二个参数为 `torch::kCUDA` 的有多少个，没有设备参数（catch-all）的有多少个。
3. **需要观察的现象**：你会看到 allreduce/elementwise/gemm/moe 等几乎全部是 `torch::kCUDA` 绑定；只有 `apply_token_bitmask_inplace_cuda` 与 `es_*` 系列是 catch-all。
4. **预期结果**：CUDA 计算算子绑 `torch::kCUDA`；少数元数据/跨后端工具函数用 catch-all。这正是「按设备分派」的体现。
5. 运行结果：本实践为纯阅读，无需运行，结论可由阅读直接得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 sgl-kernel 用 `TORCH_LIBRARY_FRAGMENT` 而不是 `TORCH_LIBRARY`？

**参考答案**：`TORCH_LIBRARY` 对同一命名空间只能定义一次；而 sgl-kernel 有多个扩展文件（common/flash/spatial/infllm 等）都要向 `sgl_kernel` 命名空间追加算子，`FRAGMENT` 允许多次打开同一命名空间累加注册，正好满足这个需求。

**练习 2**：`REGISTER_EXTENSION(common_ops)` 去掉会怎样？

**参考答案**：这个宏生成 `PyInit_common_ops`，是 Python `import` 该 `.so` 的模块入口函数。去掉后，`.so` 不再是合法的 Python 扩展模块，`import` 会报「找不到 `PyInit_common_ops`」一类的错误，扩展根本加载不起来（即便 `TORCH_LIBRARY_FRAGMENT` 已经注册了算子）。

---

### 4.2 schema 类型语法与 Tensor! 可变语义

#### 4.2.1 概念说明

`m.def` 里那段字符串就是 **schema**，它有一套类似函数签名的小语言。本模块讲清两件事：**(a) 各类型怎么写**，**(b) `Tensor!` 的可变语义以及它和 `-> ()` 的搭配**。

schema 的核心规则：

- 参数写成 `类型 名字`，逗号分隔，括号包裹。
- 末尾 `-> 返回类型` 描述返回值。
- `Tensor` 加后缀修饰语义：`?` 表示可选（optional），`!` 表示可变（mutable，即算子会原地改写它）。两者可叠加成 `Tensor!?`（可选且可变）。

#### 4.2.2 核心流程

把 schema 类型对应到 C++ 与 Python 的速查表（基于本讲源码里真实出现的写法）：

| schema 写法 | 含义 | 对应 C++ 类型 | 对应 Python |
|---|---|---|---|
| `Tensor` | 只读张量 | `at::Tensor` / `const torch::Tensor&` | `torch.Tensor` |
| `Tensor?` | 可选只读张量 | `std::optional<torch::Tensor>` | `Tensor` 或 `None` |
| `Tensor!` | **可变**张量（原地写回） | `torch::Tensor&` / `at::Tensor&`（也可按值传） | 调用前预分配的 `Tensor` |
| `Tensor!?` | 可选且可变 | `std::optional<...>` 配可变语义 | `Tensor` 或 `None` |
| `int` / `int[]` | 整数 / 整数列表 | `int64_t` / `std::vector<int64_t>` | `int` / `list[int]` |
| `float` / `bool` | 浮点 / 布尔 | `double` / `bool` | `float` / `bool` |
| `ScalarType` | dtype 枚举 | `torch::Dtype` | `torch.dtype` |
| `Tensor[]` | 张量列表 | `std::vector<at::Tensor>` | `list[Tensor]` |

返回类型：

| schema 返回 | 含义 | C++ 返回 | Python 得到 |
|---|---|---|---|
| `-> ()` | 无返回 | `void` | `None` |
| `-> Tensor` | 返回新张量 | `torch::Tensor` | `Tensor` |
| `-> int` | 返回整数 | `int64_t` | `int` |
| `-> Tensor[]` | 返回张量列表 | `std::vector<at::Tensor>` | `tuple[Tensor, ...]` |

**可变语义的执行流程**：当 schema 把某个参数标成 `Tensor!`，Dispatcher 会把这个张量视作「会被改写」——对自动求导要 bump 版本号，对 functionalization 要正确处理副作用。然后照常把同一个张量句柄传给 C++ 函数；C++ 函数直接往它的显存里写结果，调用者就能在原变量上看到新值。这就是「原地写回」。

#### 4.2.3 源码精读

**典型「原地写回」算子——rmsnorm**：

- [csrc/common_extension.cc:64-65](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L64-L65)：

```cpp
m.def("rmsnorm(Tensor! output, Tensor input, Tensor weight, float eps, bool enable_pdl) -> ()");
m.impl("rmsnorm", torch::kCUDA, &rmsnorm);
```

读这行 schema：算子名 `rmsnorm`；`output` 标了 `!` 表示会被原地写回；`input`、`weight` 只读；`eps` 是 `float`（对应 C++ `double`）；`enable_pdl` 是 `bool`；返回 `()` 即无返回。它实现的数学是：

\[ \text{output}[i] = \frac{\text{input}[i]}{\sqrt{\dfrac{1}{H}\sum_{j=1}^{H}\text{input}[j]^2 + \varepsilon}} \cdot \text{weight}[i] \]

结果不通过 `return` 给出，而是写进 `output`。对照头文件里真实的 C++ 签名 [include/sgl_kernel_ops.h:137-137](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L137-L137)：

```cpp
void rmsnorm(at::Tensor& output, at::Tensor& input, at::Tensor& weight, double eps, bool enable_pdl);
```

`output` 是 `at::Tensor&`（引用），返回 `void`——和 schema 的 `Tensor!` 与 `-> ()` 完全对应。

**多输出原地写回——moe_align_block_size**：

- [csrc/common_extension.cc:156-160](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L156-L160)：

```cpp
m.def(
    "moe_align_block_size(Tensor topk_ids, int num_experts, int block_size, Tensor! sorted_token_ids, Tensor! "
    "experts_ids, Tensor! num_tokens_post_pad, Tensor! cumsum_buffer, bool "
    "pad_sorted_token_ids, bool ignore_invalid_expert) -> ()");
m.impl("moe_align_block_size", torch::kCUDA, &moe_align_block_size);
```

这里有 **4 个 `Tensor!`**（`sorted_token_ids`、`experts_ids`、`num_tokens_post_pad`、`cumsum_buffer`），说明算子一次往 4 个预分配缓冲里写结果，返回 `()`。这正是 2.3 节说的「避免反复分配 + 地址稳定」的典型场景：MoE 对齐的这些索引缓冲在调度器里是长生命周期、需被 CUDA graph 复用的。

对照 C++ 声明 [include/sgl_kernel_ops.h:283-292](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L283-L292)：四个输出参数是 `torch::Tensor`（按值传的智能指针句柄），但因为 `torch::Tensor` 共享同一块显存，写入仍会改到调用者的原始缓冲。**要点**：可变性由 schema 的 `!` 声明，与 C++ 形参是「引用」还是「按值句柄」无关——句柄按值复制，底层显存还是同一块。

**对照：返回新张量的算子——awq_dequantize**：

- [csrc/common_extension.cc:113-114](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L113-L114)：`m.def("awq_dequantize(Tensor qweight, Tensor scales, Tensor qzeros) -> Tensor");`——三个只读 `Tensor`，`-> Tensor` 返回一个**新建**的张量，没有 `!`。这类算子由 kernel 内部分配输出。

**读/写混合——moe_sum**：

- [csrc/common_extension.cc:175-176](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L175-L176)：`m.def("moe_sum(Tensor input, Tensor! output) -> ()");`——`input` 只读、`output` 可变，一目了然地区分输入与输出。

**可选且可变——rotary_embedding 的 key**：

- [csrc/common_extension.cc:85-89](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L85-L89)：

```cpp
m.def(
    "rotary_embedding(Tensor positions, Tensor! query,"
    "                 Tensor!? key, int head_size,"
    "                 Tensor cos_sin_cache, bool is_neox) -> ()");
```

`Tensor!? key` 表示 `key` **既可选又可变**：调用时可以传 `None`（只在 query 上做 RoPE），传了的话还会被原地改写。`?` 与 `!` 的顺序在这里写作 `!?`，二者叠加表达「可能不存在，存在则改写」。

> 实际代码中的一个不一致（如实记录）：并非所有「会改写输出」的算子都严格标了 `!`。例如 [csrc/common_extension.cc:172-172](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L172-L172) 的 `moe_sum_reduce(Tensor input, Tensor output, ...) -> ()` 其实也会写 `output`，却没标 `!`；`transfer_kv_*` 家族也有类似情况。这在「这些算子不参与自动求导/不在 functionalization 路径下」时仍能正常工作，但**新写算子应当遵循 `Tensor!` + `-> ()` 的主流约定**。

#### 4.2.4 代码实践

这是本讲的核心实践（对应任务要求）。

1. **实践目标**：能对照 schema，准确列出 `rmsnorm` 与 `moe_align_block_size` 的输入/输出张量，并解释「`-> ()` + `Tensor!`」组合。
2. **操作步骤**：
   - 在 `csrc/common_extension.cc` 定位 `rmsnorm` 的 `m.def/m.impl`（第 64–65 行）。
   - 按 schema 填表：

     | 参数 | schema 类型 | 输入/输出 | 含义 |
     |---|---|---|---|
     | output | `Tensor!` | 输出（原地写回） | 归一化结果 |
     | input | `Tensor` | 输入 | 待归一化张量 |
     | weight | `Tensor` | 输入 | 缩放权重 |
     | eps | `float` | 输入 | 数值稳定小量 |
     | enable_pdl | `bool` | 输入 | 是否启用 PDL |

   - 定位 `moe_align_block_size` 的 `m.def/m.impl`（第 156–160 行），同样填表，标出 4 个 `Tensor!` 输出。
   - 回答：这两个算子为什么返回 `()` 而不是 `-> Tensor`？
3. **需要观察的现象**：两个算子的输出都在参数列表里以 `Tensor!` 出现，返回值都是 `()`。
4. **预期结果**：
   - `rmsnorm`：1 个输出（`output`），2 个张量输入；返回 `()`。
   - `moe_align_block_size`：4 个输出（`sorted_token_ids`/`experts_ids`/`num_tokens_post_pad`/`cumsum_buffer`），1 个张量输入（`topk_ids`）加 2 个 int 与 2 个 bool；返回 `()`。
   - 解释：**返回 `()` + `Tensor!` 是为了让输出缓冲由调用者预分配、地址稳定**，从而省去每次调用的显存分配、支持 CUDA graph 捕获与缓冲复用；尤其多输出的 MoE 对齐算子，用 `return` 返回多值既不便也不利于图捕获。
5. 运行结果：本实践为源码阅读，结论可直接由阅读得出。

#### 4.2.5 小练习与答案

**练习 1**：schema 里 `Tensor`、`Tensor?`、`Tensor!` 三者区别？请各举一个本讲源码里的例子。

**参考答案**：`Tensor` 只读（如 `rmsnorm` 的 `input`）；`Tensor?` 可选只读（如 `topk_softmax` 的 `correction_bias`，见 [csrc/common_extension.cc:162-165](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L162-L165)）；`Tensor!` 可变原地写回（如 `rmsnorm` 的 `output`）。

**练习 2**：为什么 `awq_dequantize` 用 `-> Tensor` 而 `rmsnorm` 用 `-> ()` + `Tensor!`？

**参考答案**：`awq_dequantize` 的输出是「一次性解量化后的权重」，由 kernel 内部新建并返回更自然；`rmsnorm` 是每个 token、每层都会高频调用的热路径，输出由调用者预分配、原地写回能避免高频分配、保持地址稳定以兼容 CUDA graph。

**练习 3**：`Tensor!?` 表达什么？源码里哪个算子用到了？

**参考答案**：表示该张量「可选且可变」——可能传 `None`，传了则会被原地改写。`rotary_embedding` 的 `key` 参数用了它（第 87 行），用于支持「只对 query 做 RoPE、不写 key」的场景。

---

### 4.3 Python 包装转发模式

#### 4.3.1 概念说明

直接调 `torch.ops.sgl_kernel.rmsnorm.default(...)` 当然可以，但 sgl-kernel 在外面又包了一层 Python 函数（`sgl_kernel.rmsnorm`、`sgl_kernel.silu_and_mul` 等）。这层包装不是多余的，它承担三件事：

1. **输入校验**：检查形状、对齐、dtype，给出友好的错误信息（schema 报错往往很底层）。
2. **输出预分配**：对 `Tensor!` 类算子，当调用者没传 `out` 时，由包装层 `torch.empty_like(...` 分配好输出缓冲再传入。
3. **转发**：一行 `torch.ops.sgl_kernel.<name>.default(...)` 把参数原样交给 Dispatcher。

某些算子还多一层 **回退（fallback）**：例如 rmsnorm 在装有 FlashInfer 且非 dynamo 编译时，优先走 FlashInfer 的实现。

> 术语：`torch.ops.sgl_kernel.rmsnorm` 是一个 **OpOverloadPacket**（算子重载包，因为一个算子名可能有多个 overload）。`.default` 取出其中的默认 overload。两者都能调用，但 `.default` 更显式、更稳，是 sgl-kernel 里的主流写法（如 `rmsnorm`、`silu_and_mul`、`moe_align_block_size` 都用 `.default`）。个别地方（如 `moe.py` 的 `fused_qk_norm_rope`）省略了 `.default`，直接调 packet，效果等价。

#### 4.3.2 核心流程

Python 包装的标准套路（以「可变输出 + 可选预分配」型算子为模板）：

```text
def wrapper(输入..., out=None):
    1. 校验形状/对齐/dtype（不满足抛 ValueError）
    2. if out is None: out = torch.empty(...)   # 为 Tensor! 预分配
    3. torch.ops.sgl_kernel.<name>.default(out, 输入...)  # 转发，out 对应 schema 的 Tensor!
    4. return out
```

带回退的套路（以 rmsnorm 为模板）：

```text
def wrapper(input, weight, eps, out=None, enable_pdl=None):
    if 有 FlashInfer 且非 dynamo 编译:
        return flashinfer 的实现          # 优先路径
    else:
        return _xxx_internal(...)          # 自家 torch.ops.sgl_kernel 路径
```

#### 4.3.3 源码精读

**rmsnorm 包装：校验 + 预分配 + 转发 + 回退**。

转发到自家 kernel 的内部函数 [python/sgl_kernel/elementwise.py:16-28](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L16-L28)：

```python
def _rmsnorm_internal(input, weight, eps, out, enable_pdl):
    if out is None:
        out = torch.empty_like(input)            # 为 Tensor! 预分配
    if enable_pdl is None:
        enable_pdl = is_arch_support_pdl()       # 默认在 Hopper 自动开 PDL
    torch.ops.sgl_kernel.rmsnorm.default(out, input, weight, eps, enable_pdl)
    return out
```

注意第 27 行把 `out` 作为**第一个参数**传入——它正好对应 schema 里的 `Tensor! output`。公开包装 [python/sgl_kernel/elementwise.py:115-122](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L115-L122) 在此之前先做 FlashInfer 回退判断：

```python
if (
    _has_flashinfer
    and input.dtype in _FLASHINFER_NORM_SUPPORTED_DTYPES
    and not torch.compiler.is_dynamo_compiling()
):
    return _flashinfer_norm.rmsnorm(input, weight, eps, out, enable_pdl)
else:
    return _rmsnorm_internal(input, weight, eps, out, enable_pdl)
```

这里 `not torch.compiler.is_dynamo_compiling()` 这一支很关键：**FlashInfer 的 JIT 加载路径在 `torch.compile(fullgraph=True)` 下不可追踪**，所以在 dynamo 编译期强制回退到自家实现，保证编译不炸（代码注释里引用了 flashinfer issue #2734）。

**silu_and_mul 包装：对齐校验 + 半宽输出预分配**。

[python/sgl_kernel/elementwise.py:258-270](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L258-L270)：

```python
def silu_and_mul(input, out=None):
    if input.shape[-1] * input.dtype.itemsize % 16 != 0:
        raise ValueError("The pointers must be multiple of 16 bytes.")  # 16 字节对齐校验
    if out is not None:
        _check_shape(input, out)
    else:
        out = torch.empty(
            input.shape[:-1] + (input.shape[-1] // 2,),                # 输出是输入「半宽」
            device=input.device, dtype=input.dtype,
        )
    torch.ops.sgl_kernel.silu_and_mul.default(out, input)
    return out
```

三件事齐全：对齐校验（CUDA kernel 用 128-bit 向量存取，故要求末维字节数是 16 的倍数）、半宽输出预分配（门控激活的 `(gate, up)` 拼接输入 → 半宽输出）、转发。`out` 同样作为第一个参数，对应 schema `silu_and_mul(Tensor! out, Tensor input) -> ()`。

**moe_align_block_size 包装：纯转发（缓冲全由调用者准备）**。

[python/sgl_kernel/moe.py:6-27](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/moe.py#L6-L27)：

```python
def moe_align_block_size(topk_ids, num_experts, block_size,
                         sorted_token_ids, experts_ids,
                         num_tokens_post_pad, cumsum_buffer,
                         pad_sorted_token_ids=False, ignore_invalid_expert=False):
    torch.ops.sgl_kernel.moe_align_block_size.default(
        topk_ids, num_experts, block_size,
        sorted_token_ids, experts_ids, num_tokens_post_pad, cumsum_buffer,
        pad_sorted_token_ids, ignore_invalid_expert,
    )
```

这个包装**没有预分配、没有校验**——4 个 `Tensor!` 输出缓冲全部由调用者（调度器）事先准备好再传入。这印证了 4.2 里说的：MoE 的索引缓冲是长生命周期、需被 CUDA graph 复用的对象，必须在更高层统一管理，所以包装层只做透传。参数顺序与 schema 一一对应（`topk_ids`→`Tensor`，4 个缓冲→4 个 `Tensor!`，2 个 bool 收尾）。

**导出层**：这些包装函数最终在 [python/sgl_kernel/__init__.py:35-50](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L35-L50)（elementwise）和 [python/sgl_kernel/__init__.py:90-100](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L90-L100)（moe）被 `from ... import` 进顶层命名空间，于是用户可以写 `sgl_kernel.rmsnorm(...)`、`sgl_kernel.moe_align_block_size(...)`。

#### 4.3.4 代码实践

1. **实践目标**：把「Python 包装 → schema → C++ 签名」三层对齐，验证参数一一对应。
2. **操作步骤**：
   - 取 `topk_softmax`：读 Python 包装 [python/sgl_kernel/moe.py:30-56](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/moe.py#L30-L56)，再读 schema [csrc/common_extension.cc:162-165](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L162-L165)。
   - 列一张三列对照表：Python 形参 / schema 参数（含类型与 `!`）/ 是否输出。
   - 思考：`correction_bias` 在 schema 里是 `Tensor?`，在 Python 里默认是什么？
3. **需要观察的现象**：Python 包装的实参顺序与 schema 参数顺序完全一致；`topk_weights`/`topk_indices` 对应 schema 的两个 `Tensor!`（输出）。
4. **预期结果**：
   - `topk_weights`、`topk_ids`(Python) → `topk_weights`、`topk_indices`(schema，均 `Tensor!`，输出)。
   - `gating_output` → `Tensor`（输入）。
   - `correction_bias` 在 schema 是 `Tensor?`，Python 默认 `None`（即可选）。
5. 运行结果：源码阅读型实践，结论由对照直接得出。可选运行（待本地验证）：若已 `pip install sglang-kernel` 且有 GPU，可在 Python 里执行 `print([n for n in dir(torch.ops.sgl_kernel) if not n.startswith('_')][:10])` 观察已注册的算子名，验证它们都来自本讲的 `m.def`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `moe_align_block_size` 的 Python 包装不做 `out = torch.empty(...)` 预分配，而 `silu_and_mul` 会做？

**参考答案**：`silu_and_mul` 是无状态的 elementwise，输出可由包装层临时分配；`moe_align_block_size` 的 4 个输出是调度器管理的长生命周期索引缓冲（要被后续 grouped GEMM 复用、要进 CUDA graph），必须由调用者统一分配并持有，包装层只透传。

**练习 2**：`torch.ops.sgl_kernel.rmsnorm.default(...)` 里的 `.default` 能不能省？

**参考答案**：能。`torch.ops.sgl_kernel.rmsnorm` 是 OpOverloadPacket，直接调用它会自动分派到默认 overload，与 `.default` 等价（`moe.py` 的 `fused_qk_norm_rope` 就是省略写法）。但写 `.default` 更显式、更稳，是 sgl-kernel 的主流约定。

**练习 3**：rmsnorm 的 Python 包装里 `not torch.compiler.is_dynamo_compiling()` 这个条件去掉会有什么后果？

**参考答案**：在 `torch.compile(fullgraph=True)` 追踪时，代码会走进 FlashInfer 的 JIT 加载路径，其中 `Path.exists()`/`os.stat()` 对 dynamo 不可追踪，会导致整图编译失败。保留该条件可在编译期强制回退到自家 `_rmsnorm_internal`，保证编译通过。

## 5. 综合实践

把三个模块串起来，完成一次「全链路定位 + 契约解读」。

**任务**：任选 sgl-kernel 里的一个算子（建议选 `fused_add_rmsnorm` 或 `topk_sigmoid`），完成下面四步，并把结果填进一张表。

1. **注册层**：在 `csrc/common_extension.cc` 里找到它的 `m.def/m.impl`，抄下 schema 字符串，判断是一行式还是两步式、是否绑定 `torch::kCUDA`。
2. **契约层**：解读 schema——标出每个 `Tensor!`（输出）、`Tensor?`（可选）、`Tensor`（只读）、`int/float/bool`，以及返回类型。
3. **声明层**：在 `include/sgl_kernel_ops.h` 里找到同名 C++ 函数，确认形参类型与 schema 对应（注意 `Tensor!` 对应 `at::Tensor&` 或按值 `torch::Tensor` 句柄）。
4. **包装层**：在 `python/sgl_kernel/elementwise.py` 或 `moe.py` 里找到 Python 包装，说明它做了哪些「校验 / 预分配 / 回退 / 转发」。

**示例答案骨架（以 `fused_add_rmsnorm` 为例）**：

- 注册层：[csrc/common_extension.cc:67-68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L67-L68)，两步式，绑定 `torch::kCUDA`。
- 契约层：`fused_add_rmsnorm(Tensor! input, Tensor! residual, Tensor weight, float eps, bool enable_pdl) -> ()`——`input`、`residual` 都是 `Tensor!`（两步原地更新：先 `residual += input`，再 `input = norm(residual)*weight`），`weight` 只读，返回 `()`。
- 声明层：[include/sgl_kernel_ops.h:138-139](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L138-L139)，`void sgl_fused_add_rmsnorm(torch::Tensor input, torch::Tensor residual, torch::Tensor weight, double eps, bool enable_pdl);`（注意 `m.impl` 绑定的函数名是 `sgl_fused_add_rmsnorm`，与 schema 算子名不同——绑定的是函数指针，名字可以不一致）。
- 包装层：[python/sgl_kernel/elementwise.py:125-162](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L125-L162)，含 FlashInfer 回退与 dynamo 编译判断，最终 `_fused_add_rmsnorm_internal` 转发到 `torch.ops.sgl_kernel.fused_add_rmsnorm.default(...)`，无返回值（原地改写 `input`/`residual`）。

完成本任务后，你就独立走通了「Python 调用 → Dispatcher → schema → C++ 实现」这条本讲主线索。

## 6. 本讲小结

- sgl-kernel 用 `TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` 打开命名空间片段，让多个扩展文件都能向 `sgl_kernel` 注册算子；`m.def` 声明契约（schema），`m.impl` 绑定设备实现，`REGISTER_EXTENSION` 生成 Python 模块入口。
- CUDA 算子普遍用「两步式」（显式 schema 字符串 + `m.impl(name, torch::kCUDA, &fn)`），因为**显式 schema 是 `torch.compile` 正确工作的前提**；少数工具函数用一行式 catch-all。
- schema 是一套小语言：`Tensor` 只读、`Tensor?` 可选、`Tensor!` 可变（原地写回），可叠加成 `Tensor!?`；返回 `-> ()` 表示无返回，`-> Tensor` 表示返回新张量。
- **`-> ()` + `Tensor!`** 是高性能推理算子的主流写法：输出由调用者预分配、地址稳定，省去高频分配、支持 CUDA graph 与缓冲复用（rmsnorm 单输出、moe_align_block_size 四输出都是如此）。
- Python 包装层承担「校验 + 输出预分配 +（可选）回退 + 转发」，最后一行总是 `torch.ops.sgl_kernel.<name>.default(...)`，其参数顺序与 schema 一一对应；`.default` 是显式取默认 overload 的主流写法。
- 可变性由 schema 的 `!` 声明，与 C++ 形参是引用还是按值句柄无关（`torch::Tensor` 句柄按值复制，底层显存仍共享）。

## 7. 下一步学习建议

本讲只讲到「分派到 C++ 函数门口」就停了。下一讲 **u2-l3「CUDA kernel 体：CHECK 宏与 dtype 分发」** 会推开那扇门，进入 `csrc/elementwise/fused_add_rms_norm_kernel.cu` 内部，看 `m.impl` 绑定的那个 C++ 函数体里：

- 如何用 `include/utils.h` 的 `CHECK_INPUT/CHECK_DIM/CHECK_EQ` 宏做运行时校验；
- 如何用 `DISPATCH_PYTORCH_DTYPE_TO_CTYPE_*` 宏在编译期按 dtype 分派到 `nv_bfloat16/nv_half/float` 模板；
- 如何获取当前 CUDA stream 并调用底层模板实现。

建议阅读顺序：先重读本讲 4.2 的 schema 速查表，再带着「schema 里的 `Tensor! output` 在 kernel 里是怎么被写入的」这个问题进入 u2-l3。后续 u3-l1（多扩展拆分）会解释为什么 `flash_ops`、`infllm_ops` 要单独成 `.so`、各自打开 `TORCH_LIBRARY_FRAGMENT`，那是本讲「片段注册」机制在工程组织上的延伸。
