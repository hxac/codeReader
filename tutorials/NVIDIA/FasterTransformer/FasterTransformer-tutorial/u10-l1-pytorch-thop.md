# PyTorch 集成：th_op 扩展

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 FasterTransformer（下称 FT）的 `th_op` 目录如何把一个 C++/CUDA 模型封装成可在 Python 里 `import` 调用的「torch 自定义类」。
- 理解「Op / IFXxx / FTXxx\<T\>」三段式封装结构，并能指出每一层各管什么事。
- 讲透 `th_utils` 里的 `get_ptr` / `convert_tensor` 是如何做到**零拷贝**把 torch tensor 变成 FT 的 `ft::Tensor`。
- 看懂 `torch::jit::class_` 与 `torch::RegisterOperators` 两种注册方式，以及 Python 端 `torch.classes.load_library` 的加载链路。
- 说明 `BUILD_PYT` 打开时编译出的 `libth_transformer.so` 是个什么样的产物、它依赖什么、与权重转换脚本如何衔接。

## 2. 前置知识

在进入本讲前，建议你已经具备以下认知（均来自前置讲义，本讲会直接复用而不重复解释）：

- **FT 的分层与统一 Tensor 抽象**（u1-l3、u2-l1）：知道 `kernels`/`layers`/`models`/`utils` 四层；知道 `ft::Tensor` 是一个**非拥有内存**的轻量描述符，字段 `{where, type, shape, data}` 全为 const，`where` 标记 `MEMORY_CPU` / `MEMORY_GPU`。
- **显存 Allocator**（u2-l2）：知道 `AllocatorType::TH` 是把分配委托给 PyTorch 的那一档后端。
- **ParallelGpt 的 forward 接口**（u6-l1）：知道模型的统一签名是 `forward(TensorMap* output, TensorMap* input, Weight* weights)`。
- **C++ 示例的入口范式**（u1-l4）：知道 FT 全库惯用「按 `data_type` 枚举 `switch` 分发到模板实例」的套路。
- **基础 C++ 与 Python 交互概念**：什么是动态链接库（`.so`）、什么是 torch 的 `Tensor`（一块显存 + 形状 + 数据类型）。

补充一个本讲会反复用到的术语：

- **零拷贝（zero-copy）**：指不搬运数据本体，只把「指向同一块显存的指针」从一方的数据结构转交到另一方。FT 与 PyTorch 之间的张量传递几乎都是零拷贝——二者用的是同一块 GPU 显存，只是各用各的「描述符」去描述它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [th_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h) | 声明 torch↔FT 张量桥接工具：`get_ptr`、`convert_shape`、`convert_tensor`，以及一组输入校验宏 |
| [th_utils.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu) | 上述桥接工具的实现，是「零拷贝」的落点 |
| [multi_gpu_gpt/ParallelGptOp.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h) | 定义 `IFGpt`、模板类 `FTGpt<T>` 与对外类 `ParallelGptOp`，含权重绑定与 forward 全流程 |
| [multi_gpu_gpt/ParallelGptOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc) | `ParallelGptOp` 的构造/forward 实现，以及文件末尾的 `torch::jit::class_` 注册代码 |
| [bert/BertOp.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.h) | BERT 的同款三段式封装（`IFBert`/`FTBert<T>`/`FasterTransformerBert`），用于对比 |
| [bert/BertOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.cc) | BERT Op 的实现，演示了带 `.def_pickle`（可序列化）的注册方式 |
| [common/GptOps.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/GptOps.cc) | 一个「自由函数」式的 op（`find_context_duplications`），演示 `torch::RegisterOperators` 另一种注册风格 |
| [CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt) | 顶层构建，`BUILD_PYT` 开关、Torch 探测、CXX11 ABI 对齐 |
| [th_op/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/CMakeLists.txt) | 把所有 `th_<model>` 聚合为最终的 `libth_transformer.so` |
| [examples/pytorch/gpt/utils/parallel_gpt.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/parallel_gpt.py) | Python 端构造 `torch.classes.FasterTransformer.ParallelGptOp` 的薄壳 |
| [examples/pytorch/gpt/gpt_example.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/gpt_example.py) | 端到端示例：加载 .so → 转权重 → 调 forward |

---

## 4. 核心概念与源码讲解

### 4.1 th_op 三段式封装：Op / IFXxx / FTXxx\<T\>

#### 4.1.1 概念说明

FT 的核心引擎（`kernels`/`layers`/`models`）是纯 C++/CUDA，与任何深度学习框架都无关。但用户的训练权重、输入输出通常都活在某个框架里——本讲专门讲 **PyTorch** 这条集成路径，入口就是 `src/fastertransformer/th_op/`。

`th_op`（`th` = Torch，`op` = Operation）目录的职责非常克制：**它只做「框架张量 ↔ FT 内部 `ft::Tensor`」的零拷贝翻译，绝不参与任何 GPU 计算**。所有真正的 kernel/GEMM 都在引擎层完成，`th_op` 只是负责「把 torch 的指针递进去、把结果指针拿出来」。

正因为职责单一，整个 `th_op` 下每一个模型 Op 都长一个样：**三段式封装**。以 GPT 为例，打开 `ParallelGptOp.h`，你会看到三个类层层包裹：

1. **对外类 `ParallelGptOp`**：继承自 `torch::jit::CustomClassHolder`，这是 PyTorch 要求自定义类必须继承的基类。它面向 Python，构造参数和 forward 参数全用 `th::Tensor` / `int64_t` 等 Python 能直接理解的类型。
2. **抽象接口 `IFGpt`**：一个纯虚类，只声明 `forward(...)` 的签名，签名里仍用 `th::Tensor`。
3. **模板实现 `FTGpt<T>`**：真正干活的类，模板参数 `T` 是权重的数据类型（`float` / `half` / `__nv_bfloat16`）。它持有 FT 的 `ParallelGptWeight<T>`、cuBLAS handle、NCCL 通信域等真实资源。

为什么要拆三层？因为 **`th::Tensor` 是无类型的（运行期才知道 dtype），而 FT 内部模型是强类型的模板（编译期就要确定 `T`）**。三段式用一个经典的「枚举 → 模板」桥梁把这两者对接：

- 对外类 `ParallelGptOp` 在构造时拿第一个权重的 `scalar_type()`（运行期枚举），用一个 `switch` 把它分发到对应的 `FTGpt<float>` / `FTGpt<half>` / `FTGpt<__nv_bfloat16>` 实例（承接 u1-l4 的 dispatch 套路）。
- 之后 `ParallelGptOp::forward` 只调用基类指针 `ftgpt->forward(...)`，再也不用关心 `T` 是什么。

这套 `XxxOp`（对外）+ `IFXxx`（接口）+ `FTXxx<T>`（模板实现）的模式在 `th_op` 下每一个模型目录里都重复出现：`bert`、`vit`、`swin`、`t5`、`bart`、`decoder`、`decoding`、`encoder` 等无一例外。

#### 4.1.2 核心流程

对外类 `ParallelGptOp` 的构造（[ParallelGptOp.cc:24-132](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L24-L132)）流程：

```text
1. 读 st_ = weights[0].scalar_type()              # 从 torch 张量拿运行期 dtype
2. for t in weights: CHECK_INPUT(t, st_)          # 校验：都在 CUDA、都连续、dtype 一致
3. 组装 gpt_variant_params（从 layernorm_eps / 激活类型等字符串参数翻译成枚举）
4. switch (st_):
       case Float    -> ftgpt = new FTGpt<float>(...)
       case Half     -> ftgpt = new FTGpt<half>(...)
       case BFloat16 -> ftgpt = new FTGpt<__nv_bfloat16>(...)   # 受 ENABLE_BF16 守卫
       default       -> throw "Wrong Tensor type."
```

整个 dispatch 的「灵魂」就是这几行 `switch`：

```cpp
switch (st_) {
    case at::ScalarType::Float:
        ftgpt = new FTGpt<float>(head_num, size_per_head, /* ... */);
        break;
    case at::ScalarType::Half:
        ftgpt = new FTGpt<half>(head_num, size_per_head, /* ... */);
        break;
#ifdef ENABLE_BF16
    case at::ScalarType::BFloat16:
        ftgpt = new FTGpt<__nv_bfloat16>(head_num, size_per_head, /* ... */);
        break;
#endif
    default:
        throw std::runtime_error("Wrong Tensor type.");
}
```

> 见 [ParallelGptOp.cc:66-131](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L66-L131)：BF16 分支被 `#ifdef ENABLE_BF16` 包起来——这承接 u1-l2 讲过的 CUDA 版本驱动条件编译，BF16 只在 CUDA ≥ 11 时才被编译进来。

`IFGpt` 接口极其精简，只定义了带 `th::Tensor` 的纯虚 `forward`：

> [ParallelGptOp.h:29-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L29-L50)：注意它的参数里大量 `th::optional<th::Tensor>`，对应 Python 端「可有可无的采样参数」（top_k、top_p、temperature 等），这正是 u8-l1 讲过的「运行期可变解码参数」在 C++ 边的落点。

对外类 `ParallelGptOp` 自己只存两个东西：`st_`（dtype 枚举）和 `IFGpt* ftgpt`（多态指针），见 [ParallelGptOp.h:545-549](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L545-L549)。它还额外保存了 `std::vector<th::Tensor> weights`——这一点很关键，**权重的显存所有权始终在 PyTorch 手里**，`ParallelGptOp` 持有这些 `th::Tensor` 只是为了延长它们的生命周期（防止 Python 端把权重回收掉），FT 内部只借用其中的指针。详见 4.5。

#### 4.1.3 源码精读

`IFGpt` 纯虚接口（[ParallelGptOp.h:29-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L29-L50)）：

```cpp
class IFGpt {
public:
    virtual ~IFGpt() {}
    virtual void forward(th::Tensor& input_ids,
                         th::Tensor& input_lengths,
                         th::Tensor& output_ids,
                         /* ... 一堆 optional 解码参数 ... */
                         th::optional<int64_t> return_cum_log_probs_opt) = 0;
};
```

对外类 `ParallelGptOp`（[ParallelGptOp.h:498-549](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L498-L549)）：继承 `th::jit::CustomClassHolder`，这是把它注册成「torch 自定义类」的前提。

#### 4.1.4 代码实践

**实践目标**：在不打开太多文件的前提下，用「模式匹配」的眼睛快速识别 `th_op` 下任意一个模型的封装结构。

**操作步骤**：

1. 打开 `src/fastertransformer/th_op/vit/ViTOp.h`（或任选一个未讲过的模型 Op）。
2. 找到三个类：对外类（继承 `CustomClassHolder`）、`IF` 开头的接口、`FT` 开头的模板类。
3. 找到构造函数里的 `switch (st_)`，数一下它支持几种 dtype（FP32/FP16/BF16）。

**需要观察的现象**：

- 你会发现 `ViTOp.h` 的骨架与 `ParallelGptOp.h` 几乎一模一样，只是模型类型换成 `ViT`、权重换成 `ViTWeight`。这印证了「`th_op` 是机械同构的薄壳」。

**预期结果**：能口头说出「`XxxOp` 持有 `IFXxx*`，构造时按 torch dtype `switch` 出 `FTXxx<T>`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IFGpt` 要存在？直接让 `ParallelGptOp` 持有 `FTGpt<T>` 不行吗？

> **答案**：因为 `ParallelGptOp` 是非模板类（它的构造参数是 Python 传来的运行期值），而 `FTGpt<T>` 是模板，`T` 在编译期才确定。非模板类无法直接持有「尚未确定 `T`」的模板对象，必须用一个不依赖 `T` 的接口（`IFGpt`）来擦除类型，靠多态指针 `IFGpt* ftgpt` 间接持有具体实例。这是 C++ 中典型的「类型擦除（type erasure）」手法。

**练习 2**：`ParallelGptOp` 把 `std::vector<th::Tensor> weights` 存为成员（[ParallelGptOp.h:548](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L548)），但 `FTGpt<T>` 内部只用裸指针。这会造成什么后果？

> **答案**：权重的真实显存归 PyTorch 所有；`ParallelGptOp` 持有 `th::Tensor` 是为了「保活」——只要这个 Op 对象还在，torch 就不会释放这些显存。如果误删 `weights` 成员，FT 拿到的裸指针就会变成野指针。这也是为什么 `FTGpt` 析构时**不** `delete` 那些权重指针（只 `delete` 自己 `new` 的 handle/algo_map）。

---

### 4.2 th_utils：torch 张量如何零拷贝变成 ft::Tensor

#### 4.2.1 概念说明

三段式封装解决了「类型对接」，但每次 `forward` 还有更具体的问题：**输入是 `th::Tensor`，模型要的是 `ft::Tensor`，怎么转？**

最朴素的办法是「拷一份」：在 GPU 上新开一块显存，把 torch tensor 的数据 `cudaMemcpy` 过去。但这完全没必要——torch tensor 本身就是一块连续 GPU 显存加一个指针，`ft::Tensor`（u2-l1 讲过）又是个**非拥有内存**的描述符 `{where, type, shape, data}`。所以正确做法是：**只造一个描述符，让它的 `data` 指针直接指向 torch tensor 那块显存**。这就是零拷贝。

`th_utils`（`th` + `utils`）就是这套桥接工具的集合，由两个文件组成：

- [th_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h)：声明 + 一组输入校验宏。
- [th_utils.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu)：实现。

零拷贝的「灵魂」只有一个函数——`get_ptr`：

```cpp
template<typename T>
inline T* get_ptr(torch::Tensor& t) {
    return reinterpret_cast<T*>(t.data_ptr());
}
```

> [th_utils.h:56-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L56-L60)：`t.data_ptr()` 返回 torch tensor 底层那块显存的首地址（`void*`），`reinterpret_cast<T*>` 只是告诉编译器「请把它当成 `T*` 来用」，不搬动任何数据。

#### 4.2.2 核心流程

把一个 `th::Tensor` 变成 `ft::Tensor`，只需要凑齐 `ft::Tensor` 的四个字段 `{where, type, shape, data}`，每一项都直接从 torch tensor 读：

| `ft::Tensor` 字段 | 来源 | 说明 |
| --- | --- | --- |
| `where`（MemoryType） | `tensor.is_cuda()` | GPU 张量→`MEMORY_GPU`，否则→`MEMORY_CPU` |
| `type`（DataType） | `getTensorType<T>()` | 由**调用方**指定的模板 `T` 决定（编译期），见 u2-l1 |
| `shape` | `convert_shape(tensor)` | 把 torch 的 `size(i)` 逐维拷成 `std::vector<size_t>` |
| `data` | `get_ptr<T>(tensor)` | 直接复用 torch 的显存首地址，零拷贝 |

这一切打包在 `convert_tensor` 里。它有两个重载：

- 单参版 `convert_tensor<T>(tensor)`：自动推断 `where`。
- 双参版 `convert_tensor<T>(tensor, memory_type)`：**强制指定** `where`（用于「我知道这是 CPU 标量参数」的场景）。

```text
convert_tensor<T>(tensor, memory_type):
    return ft::Tensor{
        memory_type,            # where
        ft::getTensorType<T>(), # type（编译期，T 已知）
        convert_shape(tensor),  # shape
        get_ptr<T>(tensor)      # data（零拷贝指针）
    }
```

#### 4.2.3 源码精读

形状转换（[th_utils.cu:23-30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu#L23-L30)）：

```cpp
std::vector<size_t> convert_shape(torch::Tensor tensor) {
    std::vector<size_t> v_shape;
    for (int i = 0; i < tensor.dim(); i++) {
        v_shape.push_back(tensor.size(i));
    }
    return v_shape;
}
```

真正的桥接函数（[th_utils.cu:50-54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu#L50-L54)）：

```cpp
template<typename T>
ft::Tensor convert_tensor(torch::Tensor tensor, ft::MemoryType memory_type) {
    return ft::Tensor{memory_type, ft::getTensorType<T>(), convert_shape(tensor), get_ptr<T>(tensor)};
}
```

> 注意 `ft::getTensorType<T>()` 是模板，在编译期就把 C++ 类型 `T` 映射成 `ft::DataType` 枚举——这就是 u2-l1 讲过的「编译期类型→枚举」桥梁。调用方必须显式写 `convert_tensor<float>(...)` 或 `convert_tensor<int>(...)`，因为只有调用方知道这块数据该被当什么类型解读。

自动推断版（[th_utils.cu:32-37](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu#L32-L37)）：

```cpp
template<typename T>
ft::Tensor convert_tensor(torch::Tensor tensor) {
    ft::MemoryType mtype = tensor.is_cuda() ? ft::MEMORY_GPU : ft::MEMORY_CPU;
    return convert_tensor<T>(tensor, mtype);
}
```

> `tensor.is_cuda()` 是 torch 提供的查询，告诉你这块数据当前在 CPU 还是 GPU。

> **必须配套的输入校验**：零拷贝成立的前提是 torch tensor **连续（contiguous）**。因为 `get_ptr` 拿到的只是首地址，FT 内部按「形状 × 步长 1」的行主序去读，一旦 tensor 非连续（例如转置过、切片过），数据就不是按预期布局排列，会读到错误数据。`th_utils.h` 提供了一组宏强制保证这一点（[th_utils.h:31-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L31-L50)）：
>
> - `CHECK_TH_CUDA(x)`：必须是 CUDA tensor。
> - `CHECK_CONTIGUOUS(x)`：必须连续。
> - `CHECK_TYPE(x, st)`：dtype 必须等于期望值。
> - `CHECK_INPUT(x, st)`：上面三条全查（[th_utils.h:35-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L35-L38)）。
>
> `ParallelGptOp::forward` 一进来就调用 `CHECK_TH_CUDA(input_ids); CHECK_CONTIGUOUS(input_ids); TORCH_CHECK(input_ids.dtype()==kInt32,...)`（[ParallelGptOp.cc:155-160](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L155-L160)），就是在零拷贝前先把关。

#### 4.2.4 代码实践

**实践目标**：亲手对照 `ParallelGptOp.cc` 与 `th_utils.h`，把「一个 torch tensor 如何零拷贝映射成 FT 的 Tensor」的三要素（指针、shape、device）讲清楚。

**操作步骤**：

1. 打开 [ParallelGptOp.h:361-372](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L361-L372)，看 `input_tensors` 里 `input_ids` 是怎么造出来的。
2. 对照 [th_utils.h:56-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L56-L60) 与 [th_utils.cu:50-54](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.cu#L50-L54)。

**需要观察的现象**：

```cpp
{"input_ids",
 ft::Tensor{ft::MEMORY_GPU,
            ft::TYPE_INT32,
            std::vector<size_t>{request_batch_size, max_input_length},
            get_ptr<int>(input_ids)}},
```

逐项对应：

- **指针**：`get_ptr<int>(input_ids)` → `reinterpret_cast<int*>(input_ids.data_ptr())`，与 torch tensor 共享同一块显存，零拷贝。
- **shape**：`{request_batch_size, max_input_length}` 直接取自 `input_ids.size(0)` / `.size(1)`（见 [ParallelGptOp.h:308-309](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L308-L309)）。
- **device**：`ft::MEMORY_GPU`，因为前面 `CHECK_TH_CUDA` 已保证它在 GPU 上。
- **type**：`ft::TYPE_INT32`，与模板 `get_ptr<int>` 一致。

**预期结果**：你能写出一句话——「FT 没有复制任何输入数据，它只是用 `{MEMORY_GPU, TYPE_INT32, shape, input_ids.data_ptr()}` 这四个字段，造了一个指向 torch 显存的描述符」。

**关于 `output_seq_len` 的对比**：注意同一个 map 里还有：

```cpp
{"output_seq_len",
 ft::Tensor{ft::MEMORY_CPU, ft::TYPE_UINT32,
            std::vector<size_t>{request_batch_size}, output_seq_len.data()}},
```

这里 `where` 是 `MEMORY_CPU`（一个 CPU `std::vector` 的指针）——这正是不用 `convert_tensor`、直接手写 `ft::Tensor{...}` 的场景：标量/小数组参数直接传 CPU 指针即可。FT 内部会据此知道它得用 `cudaMemcpy` 把这少量数据搬到 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`convert_tensor<float>(t, MEMORY_CPU)` 强制把 `where` 设成 `MEMORY_CPU`，但如果 `t` 其实是个 CUDA tensor，会发生什么？

> **答案**：`data` 指针仍是 GPU 地址（`get_ptr` 不关心），而 `where=MEMORY_CPU` 会**误导** FT。FT 在 `MEMORY_CPU` 分支里可能直接解引用该指针（host 代码访问 device 指针 → 段错误）或对它调 `cudaMemcpy`（拷贝方向错乱）。所以双参版必须由**确定知道数据位置**的调用方使用。在 `ParallelGptOp` 里它被用于把采样参数（top_k/top_p/temperature 等，Python 端常是 CPU 张量）显式标记成 CPU，见 [ParallelGptOp.h:378-405](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L378-L405)。

**练习 2**：如果用户在 Python 端传进来一个做了 `.transpose()` 的非连续 tensor，FT 会怎样？哪一道防线会拦住它？

> **答案**：FT 内部按行主序连续读，非连续 tensor 会让数据「错位」，结果错误。防线是 `CHECK_CONTIGUOUS(x)` 宏（[th_utils.h:34](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/th_utils.h#L34)），它在 forward 入口处用 `TORCH_CHECK(x.is_contiguous(), ...)` 抛出可读的 Python 异常，让用户先 `.contiguous()` 再传。

---

### 4.3 Op 注册与 Python 调用：从 .so 到 torch.classes

#### 4.3.1 概念说明

光写出 `ParallelGptOp` 类还不够——PyTorch 并不认识它。必须把类「注册」到 PyTorch 的 TorchScript 类系统里，Python 端才能 `torch.classes.FasterTransformer.ParallelGptOp(...)` 这样调用。

`th_op` 里有两套并存的注册机制：

1. **自定义类（custom class）**：用 `torch::jit::class_<T>("namespace", "Name")` 注册，Python 端通过 `torch.classes.<namespace>.<Name>` 访问，可 `new`、可调方法、可序列化。绝大多数模型 Op 走这条。
2. **自由算子（free operator）**：用 `torch::RegisterOperators("ns::op_name", &func)` 注册一个普通函数，Python 端通过 `torch.ops.<ns>.<op_name>(...)` 调用。适合「不需要保存状态的纯工具函数」。

#### 4.3.2 核心流程

每个 Op 的 `.cc` 文件末尾都有一段看似「没被调用」的代码——其实它靠**全局静态对象的构造**在动态库加载时自动执行：

```text
文件末尾:
  static auto XXX = torch::jit::class_<XxxOp>("FasterTransformer", "XxxOp")
      .def(torch::jit::init<...所有构造参数类型...>())   # 绑定构造函数
      .def("forward", &XxxOp::forward)                   # 绑定 forward 方法
      [.def_pickle(...)]                                  # 可选：序列化支持
```

Python 端两步：

```python
torch.classes.load_library("/abs/path/libth_transformer.so")  # 加载 .so，触发注册
op = torch.classes.FasterTransformer.ParallelGptOp(...)        # 实例化
out = op.forward(...)                                          # 调用
```

#### 4.3.3 源码精读

`ParallelGptOp` 的注册（[ParallelGptOp.cc:208-240](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L208-L240)）：

```cpp
static auto fasterTransformerGptTHS =
#ifdef LEGACY_THS
    torch::jit::class_<torch_ext::ParallelGptOp>("FasterTransformerParallelGptOp")
#else
    torch::jit::class_<torch_ext::ParallelGptOp>("FasterTransformer", "ParallelGptOp")
#endif
        .def(torch::jit::init<int64_t, int64_t, /* ... 26 个构造参数类型 ... */>())
        .def("forward", &torch_ext::ParallelGptOp::forward);
```

要点逐条解读：

- **`static auto XXX = ...`**：定义一个全局静态变量。C++ 规定全局对象在动态库被 `dlopen`（即 Python 调 `load_library`）时构造，其构造函数体（`torch::jit::class_<...>` 的构造）就把类注册到 TorchScript。这正是「为什么这段代码看起来没人调用却会生效」。
- **`("FasterTransformer", "ParallelGptOp")`**：两参数版本注册到命名空间 `FasterTransformer`、类名 `ParallelGptOp`，对应 Python `torch.classes.FasterTransformer.ParallelGptOp`。单参数版（`LEGACY_THS` 宏下）是给 NVIDIA 旧版 PyTorch 镜像（20.03）用的兼容路径，见 [CMakeLists.txt:88-97](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L88-L97)。
- **`.def(torch::jit::init<...>())`**：把构造函数的**参数类型列表**告诉 TorchScript，让它知道 Python 传参时该怎么匹配与转换。注意这里的类型列表必须和 `ParallelGptOp` 构造函数签名**完全一致**（`int64_t`/`double`/`std::string`/`bool`/`std::vector<int64_t>`/`std::vector<th::Tensor>`）。
- **`.def("forward", &...::forward)`**：把 `forward` 方法以名字 `"forward"` 暴露给 Python。

BERT 的注册多了一个 `.def_pickle`（[BertOp.cc:164-235](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.cc#L164-L235)），提供「序列化（`__getstate__`）」和「反序列化（`__setstate__`）」两个回调，让 BERT Op 可以被 `torch.jit.save` / `torch.jit.load`。`ParallelGptOp` 没有这层，因为它通常每次请求新建、无需落盘。

自由算子的注册（[GptOps.cc:68-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/GptOps.cc#L68-L69)）：

```cpp
static auto find_context_duplications =
    torch::RegisterOperators("fastertransformer::find_context_duplications",
                             &torch_ext::find_context_duplications);
```

这是另一种风格——不绑类，直接把函数 `torch_ext::find_context_dups` 注册成算子，Python 端用 `torch.ops.fastertransformer.find_context_duplications(input_ids)` 调用。它封装的是 u6-l3 讲过的 `invokeFindContextDups`（共享上下文去重），是个无状态工具，故用自由算子最合适。

Python 端的加载与构造（[gpt.py:507](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/gpt.py#L507) 与 [parallel_gpt.py:30-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/parallel_gpt.py#L30-L51)）：

```python
torch.classes.load_library(os.path.abspath(lib_path))   # 一次性加载 .so
self.model = torch.classes.FasterTransformer.ParallelGptOp(
    self.head_num, self.size_per_head, self.inter_size,
    self.layer_num, self.expert_num, self.moe_k, self.moe_layer_index,
    self.vocab_size, self.start_id, self.end_id,
    self.tensor_para_size, self.pipeline_para_size, self.int8_mode,
    self.layernorm_eps, self.layernorm_type, self.activation_type,
    self.has_positional_encoding, self.has_pre_decoder_layernorm,
    self.has_post_decoder_layernorm, self.has_adapters, self.adapter_inter_size,
    self.use_attention_linear_bias,
    self.weights.w, self.weights.int8_w, self.weights.scale,
    self.shared_contexts_ratio)
```

> 对照 [ParallelGptOp.cc:214-239](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L214-L239) 的 `init<...>` 类型列表，Python 这里的实参顺序与之一一对应。

#### 4.3.4 代码实践

**实践目标**：理解「.so 加载即注册」的全局静态对象机制。

**操作步骤**：

1. 在 [ParallelGptOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc) 里找到文件最末尾的 `static auto fasterTransformerGptTHS = ...`。
2. 数一数 `.def(torch::jit::init<...>())` 里列了多少个类型，与构造函数参数个数对比。

**需要观察的现象**：

- `init<...>` 里的类型数量 = 构造函数参数数量 = Python `ParallelGptOp(...)` 的实参数量，三者严格对齐。
- 这段代码没有任何函数调用者，但因为它是**全局静态变量**，会在 `load_library` 时自动执行构造。

**预期结果**：能解释「为什么改了构造函数签名就必须同步改 `init<...>`，否则 Python 端会报参数不匹配」。如果无法运行验证（缺环境），标注「待本地验证」即可。

#### 4.3.5 小练习与答案

**练习 1**：`static auto fasterTransformerGptTHS = torch::jit::class_<...>(...)` 这个变量后续从来没被读过，删掉它的名字写成 `torch::jit::class_<...>(...)` 行不行？

> **答案**：不行。去掉名字就变成「无名临时对象」，构造完立即析构——而析构会**反注册**这个类。必须用一个具名的全局变量把对象的生命周期延长到整个进程，注册才持续有效。`static` 关键字还保证它在当前编译单元（.so）内唯一、且只构造一次。

**练习 2**：`find_context_duplications` 为什么用 `torch::RegisterOperators` 而不是 `torch::jit::class_`？

> **答案**：因为它是一个**无状态**的纯函数（输入 input_ids，输出两张索引表），不需要持有权重或配置。`class_` 适合「有状态、需要先构造再多次 forward」的对象；`RegisterOperators` 适合「即调即走」的工具函数。用错了虽能实现功能，但会让 API 形态别扭（无谓地要先 `new` 一个空对象）。

---

### 4.4 forward 内部：复用 PyTorch 的 stream 与显存

#### 4.4.1 概念说明

注册和桥接解决了「怎么把类暴露出去、怎么转张量」，最后一块拼图是 `forward` 内部到底干了什么。`FTGpt<T>::forward`（[ParallelGptOp.h:269-460](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L269-L460)）的核心思想是：**不另起炉灶，而是把 PyTorch 已经建好的 CUDA 运行时设施「借」过来用**。

具体借三样东西：

1. **CUDA stream（流）**：PyTorch 当前线程的默认流。FT 所有 kernel 都提交到这条流上，保证与用户其它 torch 操作的异步顺序正确。
2. **cuBLAS handle**：PyTorch 全局的 cuBLAS 句柄，FT 把它绑定到上面的流。
3. **显存分配器**：`AllocatorType::TH`——把临时工作区（workspace）的分配委托回 PyTorch 的 allocator。

#### 4.4.2 核心流程

`FTGpt<T>::forward` 的执行步骤：

```text
1. 取 torch 当前 stream     : at::cuda::getCurrentCUDAStream().stream()
2. 取 torch cuBLAS handle   : at::cuda::getCurrentCUDABlasHandle()
3. 把 handle 绑到上面的 stream : cublasSetStream(cublasHandle, stream)
4. 建一个 TH 后端的 Allocator（临时显存委托给 PyTorch）
5. 建 cublasMMWrapper（封装 handle + workspace + algo_map，承接 u2-l3）
6. 按 T 配置 GEMM 精度（FP16/BF16/FP32）
7. 临时构造 ft::ParallelGpt<T> 模型对象
8. 组装 input_tensors / output_tensors（TensorMap）
9. try { gpt.forward(&output_tensors, &input_tensors, &gpt_weights_) }
```

#### 4.4.3 源码精读

借 stream 与 cuBLAS handle（[ParallelGptOp.h:289-294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L289-L294)）：

```cpp
auto stream                 = at::cuda::getCurrentCUDAStream().stream();
cublasHandle_t cublasHandle = at::cuda::getCurrentCUDABlasHandle();
cublasSetStream(cublasHandle, stream);
ft::Allocator<ft::AllocatorType::TH> allocator      = ft::Allocator<ft::AllocatorType::TH>();
ft::cublasMMWrapper                  cublas_wrapper = ft::cublasMMWrapper(
    cublasHandle, cublasltHandle_, stream, cublas_algo_map_, cublas_wrapper_mutex_, &allocator);
```

> `at::cuda::getCurrentCUDAStream()` 是 PyTorch C++ API，返回当前线程绑定的 CUDA 流。把 FT 的 kernel 全提交到这条流上，意味着 FT 与用户在同一个流上的其它 torch 操作（比如前后置的数据搬运）能保持正确的异步先后顺序，不会出现「FT 的 kernel 抢跑」的问题。
>
> `AllocatorType::TH`（u2-l2 讲过）是 FT Allocator 三后端之一，它把 `malloc/free` 委托给 PyTorch 的 caching allocator。这样 FT 每步 forward 申请的临时 workspace 就进了 PyTorch 的显存池，跟原生 torch op 表现一致、可被 `torch.cuda.memory_allocated()` 观测到。

组装输入/输出 TensorMap（[ParallelGptOp.h:361-435](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L361-L435)）：把上一步借到的 stream / cublas_wrapper / allocator 传给临时构造的模型：

```cpp
ft::ParallelGpt<T> gpt = ft::ParallelGpt<T>(request_batch_size, total_output_len,
    /* ... 一长串配置 ... */, stream, &cublas_wrapper, &allocator, /* ... */);
```

然后调 u6-l1 讲过的统一接口：

```cpp
gpt.forward(&output_tensors, &input_tensors, &gpt_weights_);
```

整段包在 `try / catch` 里（[ParallelGptOp.h:445-459](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L445-L459)），把 FT 抛出的 `std::runtime_error` 转成 `FT_LOG_ERROR` + `FT_CHECK(false)`，让错误能被 logger（u1-l5）记录。

输出张量在 `ParallelGptOp::forward`（外层）里**预先用 `torch::empty` 在 CUDA 上分配好**（[ParallelGptOp.cc:175-180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L175-L180)）：

```cpp
th::Tensor output_ids = torch::empty({batch_size, beam_width, total_request_output_len},
                                     torch::dtype(torch::kInt32).device(torch::kCUDA).requires_grad(false));
```

> 这意味着结果显存也是 torch 分配的、归 torch 所有；FT 通过 `get_ptr<int>(output_ids)` 拿到指针把结果写进去，forward 返回后 Python 直接拿到一个正常的 `torch.Tensor`。整个往返**没有任何 GPU 数据拷贝**。

> **与 BERT 的对比**：`FTBert<T>::forward`（[BertOp.h:165-254](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.h#L165-L254)）思路完全相同，但有两点细节差异：
>
> 1. 它用 `std::vector<ft::Tensor>` 而非 `TensorMap`（[BertOp.h:226-238](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.h#L226-L238)），是较旧的接口风格——这印证 u2-l1 提到的「新老接口并存」。
> 2. 它每次 forward 都 `new ft::Bert<T>(...)` 然后 `delete`（[BertOp.h:204, 251](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.h#L204)），构造-析构开销更大，是历史实现，不建议在新代码里照搬。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `forward` 调用里「CUDA 设施」的来源。

**操作步骤**：

1. 打开 [ParallelGptOp.h:289-294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L289-L294)。
2. 对每行标注：stream 来自哪、cublasHandle 来自哪、allocator 是哪种后端。

**需要观察的现象**：

- 三样 CUDA 设施全都来自 `at::cuda::*`（PyTorch），FT 自己 `cublasLtCreate` 的只有构造期那个 `cublasltHandle_`（[ParallelGptOp.h:92](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L92)）。

**预期结果**：能写出一句话——「FT 的 th_op forward 不创建自己的 CUDA 流，而是复用 PyTorch 当前流的 stream、cuBLAS handle 和 caching allocator，因此 FT 与 torch 在同一异步时间线上」。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接 `cudaStreamCreate` 给 FT 单独建一条流？

> **答案**：那样 FT 的 kernel 和用户其它 torch 操作会落在两条不同的流上，异步顺序无法保证——用户在 forward 后立刻 `output.cpu()` 时，可能 FT 的 kernel 还没排到。复用 torch 当前流让所有操作在同一条流上排队，时序天然正确，且省去跨流同步的开销。

**练习 2**：临时显存用 `AllocatorType::TH` 而不是 `AllocatorType::CUDA`（FT 自管显存），好处是什么？

> **答案**：TH 后端把分配委托给 PyTorch 的 caching allocator，临时 workspace 进入 torch 显存池，可被 torch 的内存统计工具观测，也避免 FT 与 torch 各自维护一套显存池导致的双倍占用。坏处是必须等到 forward 时才能拿到 torch 的 stream（`at::cuda::getCurrentCUDAStream`），所以模型对象只能在 forward 里临时构造，不能在 Op 构造期就建好。

---

### 4.5 权重搬运与 BUILD_PYT 编译产物

#### 4.5.1 概念说明

最后把链路的两端接上：

- **输入端（权重）**：用户的权重通常来自 HuggingFace / Megatron / NeMo 等训练框架，格式与 FT 不一样。需要先用转换脚本变成 FT 的「C-model」格式（一堆按 tensor parallel 切好的 `.bin` 文件，承接 u2-l5 的预切分权重），再在 Python 里读成 `torch.Tensor` 列表传给 Op。
- **输出端（编译产物）**：`-DBUILD_PYT=ON` 时，CMake 编译出一个 `libth_transformer.so`，这就是 Python `load_library` 的目标。

#### 4.5.2 核心流程

权重从磁盘到 FT 的链路：

```text
HF/Megatron checkpoint
      │  examples/pytorch/gpt/utils/huggingface_gpt_convert.py
      │  examples/pytorch/gpt/utils/megatron_ckpt_convert.py  (等转换脚本)
      ▼
FT C-model: model.layers.<i>.attention.query_key_value.weight.<rank>.bin
            (按 tensor_para 预切分，承接 u2-l5)
      │  examples/pytorch/gpt/utils/gpt.py 的 GPTWeights.load()
      │  load_to_torch() 读 .bin → torch.Tensor
      ▼
List[torch.Tensor] self.weights.w    (每张都 .cuda())
      │  ParallelGptOp(...) 构造时传入
      ▼
FTGpt<T> 构造函数: get_ptr<T>(weights_[i + k*layer_num]) 绑定到 gpt_weights_ 的 DenseWeight
```

编译产物链路：

```text
cmake .. -DBUILD_PYT=ON -DSM=80 ...
      │
      ├─ find_package(Torch)            # 找到本机 PyTorch
      ├─ 探测 torch._C._GLIBCXX_USE_CXX11_ABI，对齐 ABI
      ├─ 设 TORCH_CUDA_ARCH_LIST
      ▼
libth_transformer.so  (= th_op/CMakeLists.txt 的 th_transformer SHARED target)
      │  内含: 所有 th_<model> 静态库对象 + transformer-shared (引擎)
      │  链接: ${TORCH_LIBRARIES} + (可选) mpi + nccl
      ▼
Python: torch.classes.load_library("./lib/libth_transformer.so")
```

#### 4.5.3 源码精读

**权重绑定**——`FTGpt<T>` 构造函数把一串 `torch::Tensor` 的指针逐个挂到 `ParallelGptWeight<T>` 的叶子字段上（[ParallelGptOp.h:98-123](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L98-L123)）：

```cpp
gpt_weights_.resizeLayer(layer_num_);
for (int i = 0; i < (int)layer_num_; i++) {
    gpt_weights_.decoder_layer_weights[i]->pre_layernorm_weights.gamma =
        get_ptr<T>(weights_[i + 0 * layer_num_]);
    gpt_weights_.decoder_layer_weights[i]->pre_layernorm_weights.beta =
        get_ptr<T>(weights_[i + 1 * layer_num_]);
    gpt_weights_.decoder_layer_weights[i]->self_attention_weights.query_weight.kernel =
        get_ptr<T>(weights_[i + 2 * layer_num_]);
    /* ... 依此类推，12 个权重 × layer_num 层 ... */
}
```

> 关键观察：`weights_` 是 Python 传进来的「按层堆叠」的张量列表——前 `layer_num` 个是所有层的 `pre_layernorm.gamma`，接着 `layer_num` 个是 `pre_layernorm.beta`，再接着是 QKV kernel……因此第 `i` 层的第 `k` 类权重的下标是 `i + k * layer_num_`。这正是 u2-l5 讲过的「分配-登记-绑定」三步里的**绑定**步——只不过这里「分配」由 PyTorch 完成，FT 只把指针挂上去。

> **与 BERT 的对比**：BERT 的权重绑定用**指针算术**而非「每层一个张量」——它把所有层的同类权重拼成一个大 tensor，靠 `get_ptr<T>(_weights[0]) + hidden_dim * local_hidden_dim * (i - first_layer_index)` 偏移定位每层（[BertOp.h:95-129](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/bert/BertOp.h#L95-L129)）。两种风格都合法，GPT 的更直观、BERT 的更省张量数量。

**转换脚本**——以 `examples/pytorch/gpt/utils/huggingface_gpt_convert.py` 为代表，把 HuggingFace 权重转成 FT 的 `model.layers.<i>.attention.query_key_value.weight.<rank>.bin` 格式。读取侧在 `gpt.py` 的 `GPTWeights.load()`（[gpt.py:280-311](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/gpt.py#L280-L311)），逐个 `load_to_torch("....<tp_rank>.bin")` 拼成与 Op 构造顺序严格一致的 `w` 列表。注意文件名里的 `.<rank>.bin` 后缀正是 u2-l5 讲的 tensor parallel 预切分编码。

**编译开关 BUILD_PYT**（[CMakeLists.txt:49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L49)）：

```cmake
option(BUILD_PYT "Build in PyTorch TorchScript class mode" OFF)
```

默认 OFF，要 PyTorch 集成必须显式 `-DBUILD_PYT=ON`。开启后 CMake 做四件事（[CMakeLists.txt:242-282](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L242-L282)）：

1. **校验 PyTorch 版本** ≥ 1.5.0（[CMakeLists.txt:243-248](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L243-L248)）。
2. **`find_package(Torch REQUIRED)`**，拿到 `TORCH_LIBRARIES` 与头文件路径。
3. **对齐 CXX11 ABI**：运行一段 Python 读 `torch._C._GLIBCXX_USE_CXX11_ABI`，据此给编译器加 `-D_GLIBCXX_USE_CXX11_ABI=0/1`（[CMakeLists.txt:266-281](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L266-L281)）。
4. **同步 GPU 架构列表**到 `TORCH_CUDA_ARCH_LIST`（[CMakeLists.txt:146-184](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L146-L184)）。

> **CXX11 ABI 对齐是新手最常踩的坑**：PyTorch 1.x 用旧 ABI（`=0`），PyTorch 2.x 用新 ABI（`=1`）。如果 FT 编译时的 ABI 与本机 torch 不一致，`.so` 加载时会因 `std::string` 等 C++ 标准库符号的 ABI 不匹配而崩溃或行为异常。CMake 这段逻辑就是自动消除这个隐患——这也呼应了 u1-l2 讲过的「BUILD_PYT 自动对齐 PyTorch 的 CXX11 ABI」。

**产物 libth_transformer.so**——由 `th_op/CMakeLists.txt` 的 SHARED target 聚合而成（[th_op/CMakeLists.txt:36-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/CMakeLists.txt#L36-L69)）：

```cmake
add_library(th_transformer SHARED
            $<TARGET_OBJECTS:th_bert>
            $<TARGET_OBJECTS:th_parallel_gpt>
            $<TARGET_OBJECTS:th_utils>
            /* ... 所有 th_<model> ... */)
target_link_libraries(th_transformer PUBLIC "${TORCH_LIBRARIES}" ...)
```

> 它把所有 `th_<model>` 静态库对象打包成一个 `.so`，并链接 PyTorch 库（`${TORCH_LIBRARIES}`）。由于每个模型 Op 在文件末尾都有那段全局静态注册代码，**这一个 `.so` 加载时就会一次性把 BERT、GPT、ViT、Swin、T5、BART……所有 Op 都注册进 TorchScript**。所以一个 `.so` 就够用，不需要每个模型单独编译。`ENABLE_FP8=ON` 时还会额外挂上 `th_gpt_fp8`（[th_op/CMakeLists.txt:71-74](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/CMakeLists.txt#L71-L74)）。

> 多 GPU 时还要加 `-DBUILD_MULTI_GPU=ON`，这会给 `.so` 额外链接 `-lmpi` 与 `${NCCL_LIBRARIES}`（顶层 [CMakeLists.txt:419-424](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L419-L424)），承接 u7 的并行依赖。

#### 4.5.4 代码实践

**实践目标**：从零跑通「编译 .so → 加载 → 调用」的最短路径，并能描述 `.so` 里有什么。

**操作步骤**：

1. 按 docs/gpt_guide.md 的 Build 指引，执行大致如下的命令（具体路径以本机为准，**待本地验证**）：

   ```bash
   mkdir build && cd build
   cmake .. -DBUILD_PYT=ON -DBUILD_MULTI_GPU=ON -DSM="80" \
            -DCMAKE_BUILD_TYPE=Release
   make -j12
   ```

2. 编译产物在 `build/lib/libth_transformer.so`（`gpt_example.py` 默认 `--lib_path ./lib/libth_transformer.so`，见 [gpt_example.py:65-66](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/gpt_example.py#L65-L66)）。

3. 用 `ldd` 看它的依赖：

   ```bash
   ldd build/lib/libth_transformer.so | grep -E "torch|nccl|mpi|cublas"
   ```

4. 运行示例（需先按 docs 转好权重与 vocab，**待本地验证**）：

   ```bash
   python examples/pytorch/gpt/gpt_example.py \
       --lib_path build/lib/libth_transformer.so \
       --ckpt_path <转换后的 C-model 路径> \
       --vocab_file ... --merges_file ...
   ```

**需要观察的现象**：

- `ldd` 输出里能看到 `.so` 依赖 `libtorch_cuda`、`libc10_cuda`、`libcublas`、（多 GPU 时）`libnccl` / `libmpi`。这说明它是一个**链接了 PyTorch 库、可被 `torch.classes.load_library` 加载的扩展模块**。
- 程序输出 `[INFO] Device ...`（来自 [ParallelGptOp.h:257](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L257) 的 `FT_LOG_INFO`），证明 Op 构造成功。

**预期结果**：能口头描述——「`libth_transformer.so` 内含所有模型的 th_op 注册代码与 FT 引擎（transformer-shared），链接 PyTorch 库；加载它即把 `FasterTransformer.ParallelGptOp`、`FasterTransformer.Bert` 等全部类注册进 TorchScript」。如果环境不具备，标注「待本地验证」。

> **小坑提醒**：若运行时报 `undefined symbol: ...` 或加载即 segfault，首先怀疑 CXX11 ABI 没对齐（重跑 cmake 看 `-- USE_CXX11_ABI=` 输出）或 SM 架构没覆盖本机 GPU。

#### 4.5.5 小练习与答案

**练习 1**：`gpt.py` 把权重读成 `List[torch.Tensor]` 后传给 `ParallelGptOp(...)`，这些 tensor 的显存归谁所有？FT 析构时会 free 它们吗？

> **答案**：归 PyTorch 所有（torch 的 caching allocator 管理）。FT 通过 `get_ptr` 只拿到裸指针，`FTGpt<T>` 析构时只 `delete` 自己 `new` 的 `cublasltHandle_` / `cublas_algo_map_` / `cublas_wrapper_mutex_`（[ParallelGptOp.h:260-267](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L260-L267)），**不** free 权重指针。真正的释放发生在 Python 端 `weights` 列表被回收时。这就是「所有权留在框架、FT 只借用」的设计。

**练习 2**：为什么 CMake 在 BUILD_PYT 时要读 `torch._C._GLIBCXX_USE_CXX11_ABI` 并据此设编译宏？

> **答案**：因为 FT 与 PyTorch 在 `.so` 边界上会交换 C++ 标准库类型（如 `std::string`、`std::vector`）。新旧 CXX11 ABI 下这些类型的内存布局不同，混用会导致符号能链接但运行时数据错乱或崩溃。自动探测并对齐 ABI，是让 FT 编译产物能被本机 PyTorch 正确加载的硬性前提。

---

## 5. 综合实践

把本讲五个模块串起来，完成一个**端到端调用链追踪**任务：

1. **从 Python 出发**：读 [gpt_example.py:254-271](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/gpt_example.py#L254-L271)，写下 `gpt(start_ids=..., output_len=...)` 这一行背后发生了什么。
2. **追踪到 Op 构造**：[parallel_gpt.py:30-51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/parallel_gpt.py#L30-L51) 把 27 个参数传给 `torch.classes.FasterTransformer.ParallelGptOp`；在 [ParallelGptOp.cc:24-132](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L24-L132) 找到 `switch(st_)` 把它分发到 `FTGpt<T>`。
3. **追踪权重搬运**：在 [ParallelGptOp.h:98-123](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L98-L123) 看到权重指针被绑定到 `gpt_weights_`。
4. **追踪一次 forward 的零拷贝**：在 [ParallelGptOp.cc:139-204](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L139-L204) 看 `input_ids` 经过 `CHECK_*` 后，在 [ParallelGptOp.h:361-372](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L361-L372) 经 `get_ptr<int>` 零拷贝进 `input_tensors`。
5. **追踪 CUDA 设施复用**：在 [ParallelGptOp.h:289-294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.h#L289-L294) 确认 stream / cublas / allocator 都借自 PyTorch。
6. **回到 Python**：[ParallelGptOp.cc:175-180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/multi_gpu_gpt/ParallelGptOp.cc#L175-L180) 预分配的 `output_ids` 被 FT 写入后，作为普通 `torch.Tensor` 返回。

最终产出一幅「Python → load_library → Op 构造(switch dispatch) → 权重绑定(get_ptr) → forward(stream 复用 + 零拷贝 TensorMap) → torch.Tensor 返回」的完整数据流图。整条链路里 GPU 数据拷贝次数为 **0**。

## 6. 本讲小结

- `th_op` 是 FT 的 **PyTorch 外壳层**，职责单一：做 torch 张量与 FT `ft::Tensor` 之间的零拷贝翻译，不参与任何 GPU 计算。
- 每个模型 Op 都是**三段式封装**：对外类 `XxxOp`（继承 `CustomClassHolder`）+ 接口 `IFXxx` + 模板实现 `FTXxx<T>`；构造时用 `switch(scalar_type)` 把运行期 dtype 分发到模板实例（类型擦除）。
- **零拷贝核心**是 `th_utils` 的 `get_ptr<T>(t)`（取 `data_ptr`）与 `convert_tensor<T>`（造 `{where, type, shape, data}` 描述符），FT 始终借用 torch 的显存，不复制数据；前提是 tensor 连续，由 `CHECK_INPUT` 系列宏把关。
- Op 靠文件末尾的 **全局静态 `torch::jit::class_<...>`** 在 `.so` 加载时自动注册，Python 端 `torch.classes.load_library` + `torch.classes.FasterTransformer.XxxOp` 使用；另有 `torch::RegisterOperators` 用于无状态自由函数（如 `find_context_duplications`）。
- `forward` 内部**复用 PyTorch 的 CUDA stream、cuBLAS handle 与 caching allocator**（`AllocatorType::TH`），保证 FT 与 torch 在同一异步时间线上；输出张量也由 torch 预分配。
- `-DBUILD_PYT=ON` 编译出**单一** `libth_transformer.so`（聚合所有 `th_<model>` + 引擎 + 链接 PyTorch 库），加载它即注册全部 Op；CMake 自动对齐 CXX11 ABI 与 GPU 架构。

## 7. 下一步学习建议

- **下一讲 u10-l2（TensorFlow 集成：tf_op）**：对比 `tf_op` 的 `BaseOp` 抽象与注册方式，体会「同一套 FT 引擎，不同框架外壳」的设计。
- **u10-l3（Triton backend 部署）**：看 `triton_backend` 如何把同样的模型包装成 Triton inference server 的自定义 backend，那是面向生产服务的另一条集成路径。
- **源码延伸阅读**：
  - [th_op/common/DynamicDecodeOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/th_op/common/DynamicDecodeOp.cc)：把 u8 的动态解码层也暴露成 torch op。
  - [examples/pytorch/gpt/utils/huggingface_gpt_convert.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/gpt/utils/huggingface_gpt_convert.py)：真实权重转换脚本，看 HF → FT C-model 的细节。
  - [examples/pytorch/bert/bert_example.py](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/pytorch/bert/bert_example.py)：BERT 端到端示例，对照本讲对 BERT Op 的差异讲解。
