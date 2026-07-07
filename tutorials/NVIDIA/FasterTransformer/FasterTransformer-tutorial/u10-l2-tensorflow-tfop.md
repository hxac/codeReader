# TensorFlow 集成：tf_op

## 1. 本讲目标

本讲承接 u10-l1（PyTorch 集成：th_op），讲解 FasterTransformer（以下简称 FT）如何把自己封装成 **TensorFlow 自定义 OP（custom op）**，让用户能在原生 TensorFlow 图（graph）里像调用一个普通算子那样调用 FT 的 BERT / GPT / Decoder / Decoding。

学完本讲，你应该能够：

- 说清 TensorFlow 自定义 OP 的生命周期（`REGISTER_OP` 声明 → `OpKernel` 子类 → `REGISTER_KERNEL_BUILDER` 注册 GPU 实现）。
- 读懂 `BaseOp` 抽象基类如何复用 cuBLAS handle、如何把 TF 的 `tf::Tensor` 零拷贝转成 FT 的 `ft::Tensor`。
- 对照 `BertOp` / `GptOp` 等具体 OP，描述一次 `Compute` 调用里「取流 → 建 allocator/wrapper → 装权重 → 构模型 → forward」的完整流程。
- 理解 `BUILD_TF` / `BUILD_TF2` / `TF_PATH` 三个编译开关，以及 TF1 与 TF2 在 C++ ABI 上的关键差异。
- 写出编译 TF 模式所需的 cmake 命令。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 TensorFlow 自定义 OP 是什么

TensorFlow 的算子（如 `MatMul`、`Conv2D`）在底层都是 C++ 写的 `OpKernel`。NVIDIA 官方提供了写自定义 OP 的标准三件套：

1. **声明算子签名**：用 `REGISTER_OP("名字")` 描述这个算子有几个输入、几个输出、有哪些属性（`Attr`）、输出形状如何推断（`SetShapeFn`）。这是给 TF 的「图构造期」看的。
2. **实现算子逻辑**：继承 `tf::OpKernel`，重写 `Compute(tf::OpKernelContext* context)`。这是给 TF 的「执行期」调用的，每次图跑到这个算子就调用一次。
3. **注册到设备**：用 `REGISTER_KERNEL_BUILDER` 把算子名 + 设备（`DEVICE_GPU`）+ 类型约束（`TypeConstraint<T>`）绑在一起，告诉 TF「这个算子在 GPU 上由这个类执行」。

编译后得到一个 `.so` 共享库，在 Python 里用 `tf.load_op_library("tf_bert.so")` 加载，之后就能在图里用 `module.bert(...)` 调用。

### 2.2 tf::Tensor 与 ft::Tensor 的区别

- `tf::Tensor` 是 TensorFlow 的张量，它的数据可能存放在 CPU 或 GPU，由 TF 的运行时（runtime）管理内存。
- `ft::Tensor`（u2-l1 已讲）是 FT 的「轻量描述符」，本身**不拥有内存**，只记录 `where`（MEMORY_CPU / MEMORY_GPU）、`type`、`shape` 和一个裸指针 `data`。

两者之间做转换的**关键技巧是零拷贝**：不复制数据，只是把 `tf::Tensor` 里那块 GPU 显存的指针，包装成 `ft::Tensor` 的 `data` 字段。这样 FT 的 kernel 就能直接在 TF 申请好的显存上读写，省掉一次 host↔device 拷贝。

### 2.3 GPU stream 与 cuBLAS handle

TensorFlow 在 GPU 上跑图时，每个算子的 `Compute` 都能从 `context->eigen_device<GPUDevice>().stream()` 拿到当前算子所在的 **CUDA stream**。FT 的 kernel、cuBLAS GEMM 都必须挂到这条 stream 上，才能保证和图里其它算子的执行顺序正确。所以 OP 一进来，第一件事就是 `cublasSetStream(cublas_handle, stream)`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/fastertransformer/tf_op/BaseOp.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h) | 所有 TF OP 的抽象基类，封装 cuBLAS handle、TF→FT 张量转换 |
| [src/fastertransformer/tf_op/bert/BertOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc) | BERT 的 TF OP（旧式 `vector<Tensor>` 接口的代表） |
| [src/fastertransformer/tf_op/encoder/EncoderOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/encoder/EncoderOp.cc) | Encoder 的 TF OP（复用 `Bert` 类，pre-LN + Relu） |
| [src/fastertransformer/tf_op/gpt/GptOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/gpt/GptOp.cc) | GPT 的 TF OP（新式 `TensorMap` 接口的代表） |
| [src/fastertransformer/tf_op/decoding/DecodingOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/decoding/DecodingOp.cc) | Decoding 的 TF OP，使用 `ft::TensorMap` |
| [src/fastertransformer/tf_op/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/CMakeLists.txt) | tf_op 子目录的构建：按 `BUILD_TF` / `BUILD_TF2` 分组编译各模型 |
| [src/fastertransformer/tf_op/bert/CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/CMakeLists.txt) | bert OP 的具体编译目标（产出 `tf_bert.so`） |
| [CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt) | 顶层构建：`BUILD_TF` / `BUILD_TF2` / `TF_PATH` 选项与 ABI 宏 |
| [src/fastertransformer/utils/allocator.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h) | `Allocator<AllocatorType::TF>`：把 FT 的临时显存申请委托给 TF 的 `context->allocate_temp` |

## 4. 核心概念与源码讲解

### 4.1 BaseOp：所有 TF OP 的抽象基类

#### 4.1.1 概念说明

每一个 FT 的 TF OP（`BertOp`、`EncoderOp`、`GptOp`、`DecoderOp`、`DecodingOp`……）都要做三件重复的事：

1. 在构造时创建 cuBLAS / cuBLASLt handle，并准备一把保护 handle 的互斥锁（因为 cuBLAS handle 不是线程安全的，见 u2-l3）。
2. 在析构时销毁这些 handle。
3. 在 `Compute` 里反复地把 TF 的输入张量转成 FT 的 `ft::Tensor`，并取出 cuBLAS handle。

为了避免每个 OP 重复写这些样板代码，FT 抽象出一个模板基类 `BaseOp<T>`。`T` 是 TF 侧的元素类型（`Eigen::half`、`Eigen::bfloat16`、`float`）。`BaseOp` 继承自 `tf::OpKernel`，所以它的子类天然满足「TF OP 必须继承 `OpKernel`」的要求。

#### 4.1.2 核心流程

`BaseOp` 的职责可以用下面这个伪代码概括：

```
BaseOp<T> : public tf::OpKernel
  构造:  cublasCreate / cublasLtCreate / new mutex
  析构:  cublasDestroy / cublasLtDestroy / delete mutex
  提供:
    get_tensor(...)            # 从 TF input 取出类型化裸指针
    convert_shape(tf_tensor)   # tf::Tensor 的形状 -> vector<size_t>
    convert_tensor(tf_tensor)  # tf::Tensor -> ft::Tensor（零拷贝）
    convert_int_tensor(...)    # int32 张量的零拷贝转换
    get_cublas_handler()       # 暴露 handle 给子类
    get_cublas_wrapper_mutex() # 暴露互斥锁给子类
```

`convert_tensor` 是最关键的方法：它根据模板参数 `T` 走 `std::is_same` 分支，把 TF 的存储类型映射到 FT 的计算类型（`Eigen::half → half`、`Eigen::bfloat16 → __nv_bfloat16`、`float → float`），然后用 FT 张量的构造函数直接接管 `tf::Tensor` 的数据指针，`where` 固定为 `MEMORY_GPU`。

#### 4.1.3 源码精读

先看类的声明与构造/析构。`BaseOp` 继承 `tf::OpKernel`，构造时创建 handle 和锁，析构时释放：

[BaseOp.h:L44-L64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h#L44-L64) —— `BaseOp` 继承 `tf::OpKernel`，构造里 `cublasCreate` / `cublasLtCreate` 并 `new std::mutex`，析构里成对销毁。注意所有 CUDA 调用都包在 `try/catch` 里，捕获后用 `OP_REQUIRES` 把异常转成 TF 的错误状态。

再看核心的张量转换方法 `convert_tensor`：

[BaseOp.h:L99-L121](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h#L99-L121) —— 这里用 `std::is_same<T, ...>::value` 做编译期分支：当 `T == Eigen::half` 时返回一个 `ft::Tensor{MEMORY_GPU, getTensorType<half>(), shape, (half*)data}`。关键在于 `(half*)(tensor.flat<T>().data())` —— 它取出 TF 张量底层 GPU 显存的指针，强转成 FT 期望的类型后直接塞进 `ft::Tensor`，**没有任何数据拷贝**。`where` 写死为 `MEMORY_GPU`，因为 TF 的 GPU kernel 上下文里数据天然在显存。BF16 分支被 `#ifdef ENABLE_BF16` 守卫（承接 u1-l2 的条件编译）。

`convert_shape` 负责把 TF 的多维形状铺平成 FT 习惯的 `vector<size_t>`：

[BaseOp.h:L88-L96](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h#L88-L96) —— 遍历 `tensor.dims()`，把每一维的 `dim_size` 收集进 vector。`FT_CHECK(tensor.dims() != -1)` 是防御未初始化张量。

`get_tensor` 是给子类装权重时用的便捷方法，把第 `tensor_id` 个 TF 输入转成类型化指针：

[BaseOp.h:L67-L85](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h#L67-L85) —— 它用 `reinterpret_cast` 把 `context->input(tensor_id).flat<T>().data()` 转成目标类型指针，再用 `OP_REQUIRES` 校验非空。有 `const` 与非 `const` 两个重载，以及一个针对 `int` 的特化。

最后是私有的成员字段与访问器：

[BaseOp.h:L129-L147](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/BaseOp.h#L129-L147) —— 三个私有成员：`cublas_handle_`、`cublaslt_handle_`、`cublas_wrapper_mutex_`，通过 getter 暴露给子类。一个 `BaseOp` 实例对应一个 OP 实例，这些 handle 在 OP 的整个生命周期内复用。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，确认 `convert_tensor` 真的是零拷贝。

**操作步骤**：

1. 打开 `src/fastertransformer/tf_op/BaseOp.h` 的 `convert_tensor`（第 99–121 行）。
2. 对照 u2-l1 讲过的 `ft::Tensor` 构造函数签名 `Tensor(MemoryType where, DataType type, vector<size_t> shape, void* data)`。
3. 思考：如果改成「先 `cudaMalloc` 一块新显存、再 `cudaMemcpy` 过去」，会多出几次显存读写？

**需要观察的现象**：

- `convert_tensor` 全程没有出现 `cudaMalloc` / `cudaMemcpy` / `new`。
- `ft::Tensor` 拿到的 `data` 指针就是 TF 张量的原始指针。

**预期结果**：零拷贝意味着 FT 算子直接读写 TF 分配的显存，整个 OP 不引入任何额外的 device↔device 或 host↔device 数据搬运。如果未来 FT kernel 把这块显存写坏，TF 侧的张量也会被污染——这是共享显存换性能的代价。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseOp` 要把 cuBLAS handle 放在基类、而不是每个 OP 在 `Compute` 里临时创建？

**参考答案**：`cublasCreate` / `cublasDestroy` 是相对昂贵的操作，而一个 OP 实例会在图里被调用成千上万次。把 handle 放在基类成员里，构造一次、反复复用，避免每次 `Compute` 都付创建销毁的开销。这正是 u2-l3 提到的「handle 复用」思想。

**练习 2**：`convert_tensor` 为什么把 `where` 写死成 `MEMORY_GPU`，而不是根据 TF 张量实际位置判断？

**参考答案**：因为这些 OP 只注册在 `DEVICE_GPU` 上（见 4.2.3 的 `REGISTER_KERNEL_BUILDER`），TF 保证进入 `Compute` 的张量都在 GPU 显存里，不会出现 CPU 张量。所以无需判断，直接标记为 `MEMORY_GPU`。

---

### 4.2 一个完整的 TF OP：BertOp 的注册与执行

#### 4.2.1 概念说明

`BaseOp` 只是地基，真正能被 TF 调用的 OP 必须完成「声明 + 实现 + 注册」三步。本节以 `BertOp` 为例，这是最经典、结构最清晰的 FT TF OP。

注意一个细节：TF 侧的元素类型是 `Eigen::half`（TF 自带的半精度包装类型），而 FT 内部用的是 CUDA 原生的 `half` / `__nv_bfloat16`。两者虽然位宽相同，但 C++ 类型不同，需要一个小的映射器 `TFTraits` 把 TF 类型「翻译」成 FT 类型。

#### 4.2.2 核心流程

`BertOp` 的完整生命周期：

```
1. 图构造期：REGISTER_OP("Bert") 声明 19 个输入（含 N 个层的权重）+ 1 个输出 + 7 个 Attr
   ↓
2. OP 实例化（首次调用前一次）：BertOp 构造函数
   - 调 BaseOp 构造（建 handle）
   - GetAttr 读 head_num / size_per_head / inter_size / num_layer / remove_padding / q_scaling
   - getSMVersion() 查 GPU 架构
   - new cublasAlgoMap("gemm_config.in") 加载离线调优算法表（承接 u2-l4）
   ↓
3. 每次 Compute：
   a. 校验输入个数 == num_layer * 16 + 3
   b. 从 input[0] 读 batch_size / from_seq_len
   c. 取 stream，cublasSetStream
   d. 建 Allocator<TF>（委托 TF 分配临时显存）
   e. 建 cublasMMWrapper，按 T 设 GEMM 精度（setFP16GemmConfig 等）
   f. for 每层：用 get_tensor 把权重指针挂进 BertWeight
   g. getAttentionType 推导注意力实现（承接 u3-l3）
   h. 构造 ft::Bert 模型对象
   i. convert_tensor 把 input[0]/input[2] 转成 ft::Tensor
   j. allocate_output 拿到输出张量
   k. bert.forward(&output_tensors, &input_tensors, &bert_weights)
```

#### 4.2.3 源码精读

**第一步：声明算子签名。** `REGISTER_OP("Bert")` 描述了这个 OP 的「接口契约」：

[BertOp.cc:L26-L58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L26-L58) —— 注意几个要点：输入里大量 `N * T` 表示「一个长度为 N 的 list，每个元素类型是 T」，例如 `attr_q_kernel: N * T` 代表 N 层各自的 Q 权重，N 由 `Attr("N: int")` 给出；`Attr("T: {float, half}")` 限定本 OP 只支持 float 和 half 两种类型；`SetShapeFn` 里 `c->set_output(0, c->input(0))` 声明输出形状与第一个输入相同（BERT 输入输出隐状态同形）。这个声明只跑一次，TF 用它做图构造期的形状推断。

**第二步：TFTraits 类型映射器。** 把 TF 的 `Eigen::half` / `Eigen::bfloat16` 映射成 FT 的 `half` / `__nv_bfloat16`：

[BertOp.cc:L60-L81](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L60-L81) —— 模板特化 `TFTraits<float>::DataType = float`、`TFTraits<Eigen::half>::DataType = half`、（BF16 守卫下）`TFTraits<Eigen::bfloat16>::DataType = __nv_bfloat16`。后面 `typedef typename traits_::DataType DataType` 就拿到了 FT 侧真正用来实例化模型的类型。

**第三步：构造函数读属性。** OP 实例化时（每个图节点一次），从 `context` 取出静态属性并加载算法表：

[BertOp.cc:L83-L106](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L83-L106) —— `OP_REQUIRES_OK(context, context->GetAttr(...))` 是 TF 读属性的标准写法，失败时把错误注入 context。`ft::getSMVersion()` 在运行期查询 GPU 的 SM 版本（承接 u3-l3 的 `getAttentionType` 决策）。`new ft::cublasAlgoMap("gemm_config.in")` 读取离线调优产物（承接 u2-l4）。

**第四步：Compute 的环境准备。** 取流、配 handle、建 allocator 与 wrapper：

[BertOp.cc:L121-L142](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L121-L142) —— `context->eigen_device<Device>().stream()` 拿到当前 CUDA stream，随后 `cublasSetStream` 把 cuBLAS 挂到该 stream。`ft::Allocator<ft::AllocatorType::TF> allocator(context, stream)` 是关键一行——它让 FT 的临时显存申请走 TF 的分配器（见 4.3）。然后按 `T` 选 GEMM 精度配置（FP16 / BF16 / FP32）。

**第五步：装权重。** 用基类的 `get_tensor` 把 19 个输入里属于权重的部分，按 `3 + num_layer * k + i` 的下标公式挂进 `BertWeight`：

[BertOp.cc:L144-L189](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L144-L189) —— 注意下标规律：前 3 个输入是 `from_tensor / to_tensor / sequence_length`，从第 4 个（下标 3）开始才是权重。权重按「类型 × 层」组织，同类 N 层连续排列，所以第 k 类第 i 层的下标是 `3 + num_layer * k + i`。这里 FT 并不复制权重，只是把 TF 张量的指针绑到 `BertWeight` 的叶子指针上（承接 u2-l5 的「指针树」组织）。末尾把 `post_transformer_layernorm_weights` 显式置空（BERT base 不用）。

**第六步：构造模型并 forward。**

[BertOp.cc:L191-L226](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L191-L226) —— `getAttentionType` 据 `(size_per_head, sm, remove_padding, from_seq_len)` 自动选 FUSED/UNFUSED（承接 u4-l1）。随后 `context->allocate_output` 让 TF 分配输出张量，`convert_tensor` 把输入转成 `ft::Tensor`，最后 `bert.forward(&output_tensors, &input_tensors, &bert_weights)` 真正执行。注意 forward 包在 `try/catch` 里，把 FT 抛出的 `std::runtime_error` 转成错误输出。

**第七步：注册 GPU kernel。**

[BertOp.cc:L248-L256](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L248-L256) —— 宏 `REGISTER_GPU(T)` 展开成 `REGISTER_KERNEL_BUILDER(Name("Bert").Device(DEVICE_GPU).TypeConstraint<T>("T"), BertOp<GPUDevice, T>)`，随后对 `float` 和 `Eigen::half` 各实例化一次。`#ifdef GOOGLE_CUDA` 守卫保证只在 GPU 编译时注册。

#### 4.2.4 代码实践

**实践目标**：理清 `BertOp` 一个 OP 实例里「构造期」与「执行期」各自做了什么。

**操作步骤**：

1. 读 [BertOp.cc:L83-L101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L83-L101) 的构造函数。
2. 读 [BertOp.cc:L108-L236](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L108-L236) 的 `Compute`。
3. 列两张表：构造期动作 vs 每次 Compute 的动作。

**需要观察的现象**：

- 构造期只做一次：读属性、查 SM、加载 `gemm_config.in`、建 handle。
- 每次 Compute 都做：取流、装权重、构模型、forward。注意 `Bert` 模型对象是**每次 Compute 都新建**的（栈上局部变量）。

**预期结果**：构造期开销摊薄到所有调用，而 `Compute` 里仍有相当多重复工作（重建 wrapper、重建模型对象）。这是 TF OP 实现的典型取舍——优先保证接口简单与线程安全。如果想优化，可像 Triton backend 那样把模型对象缓存到 OP 成员里（见 u10-l3）。

#### 4.2.5 小练习与答案

**练习 1**：`BertOp` 的输入数量校验是 `num_layer_ * 16 + 3`，请解释这个 16 和 3 分别是什么。

**参考答案**：`3` 是非权重输入：`from_tensor`、`to_tensor`、`sequence_length`。`16` 是**每一层**贡献的权重张量数：Q/K/V/output 各 kernel+bias 共 8 个，attn layernorm 的 beta+gamma 2 个，FFN intermediate/output 各 kernel+bias 共 4 个，FFN layernorm 的 beta+gamma 2 个，合计 8+2+4+2=16。

**练习 2**：为什么 `Bert` 模型对象要在 `Compute` 里每次新建，而不是放进构造函数成员里复用？

**参考答案**：因为 `batch_size` 和 `from_seq_len` 是从运行期输入张量的形状读出来的（[BertOp.cc:L114-L115](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L114-L115)），每次调用可能不同。`ft::Bert` 的构造参数里包含这两个维度，所以无法在构造期固定。这也是 TF OP 与 Triton backend（固定 max_batch）的一个设计差异。

---

### 4.3 TF Tensor 到 FT Tensor 的零拷贝转换与 TF Allocator

#### 4.3.1 概念说明

上一节看到 `BertOp` 在 `Compute` 里做了两件和「内存」相关的事：

1. **权重与输入**：用 `convert_tensor` / `get_tensor` 把 TF 已有的张量指针直接绑给 FT，零拷贝。
2. **临时显存**：FT 的各层 forward 需要工作区（workspace，承接 u3-l5），这些显存由谁分配？

答案是：FT 用 `Allocator<AllocatorType::TF>` 这条后端（承接 u2-l2 的 `AllocatorType` 三态），把临时显存申请**委托给 TensorFlow**。这样 FT 的工作区就成了 TF 内存池的一部分，TF 可以统一管理生命周期、和图里其它算子共享内存。

#### 4.3.2 核心流程

`Allocator<AllocatorType::TF>` 的核心机制：

```
malloc(size):
  调用 context_->allocate_temp(DT_UINT8, TensorShape{对齐到 32 的 size}, &buf)
  把 buf（一个 tensorflow::Tensor）登记进 pointer_mapping_（地址 -> tensorflow::Tensor）
  返回 buf 的裸指针
  注意：tensorflow::Tensor 由 TF 持有，FT 只是借用指针

free(ptr):
  从 pointer_mapping_ 删掉对应条目
  tensorflow::Tensor 出作用域后由 TF 自动回收
```

这里有一个微妙之处：TF 的 `allocate_temp` 返回的是一个 `tensorflow::Tensor` 对象（RAII），FT 拿到的是它内部的指针。为了防止 `tensorflow::Tensor` 被提前析构导致指针悬空，FT 把它存在 `pointer_mapping_` 这个 map 里，直到 `free` 时才擦除。

#### 4.3.3 源码精读

`Allocator<AllocatorType::TF>` 的定义在 `allocator.h`，受 `#ifdef GOOGLE_CUDA` 守卫：

[allocator.h:L273-L306](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L273-L306) —— 它继承自 `IAllocator`（承接 u2-l2），持有三个成员：`context_`（TF 的 OpKernelContext）、`pointer_mapping_`（地址→`tensorflow::Tensor` 的账本）、`stream_`。构造时新建空账本。

`malloc` 的实现——把 FT 的分配请求转成 TF 的 `allocate_temp`：

[allocator.h:L318-L346](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L318-L346) —— 几个要点：①把请求大小 `ceil` 到 32 的倍数（`buf_size = ceil(size/32)*32`），便于对齐；②`context_->allocate_temp(DT_UINT8, TensorShape{buf_size}, &buf)` 是 TF 分配临时 GPU 显存的标准 API；③若 `is_host` 为真，则设 `on_host + gpu_compatible` 走 pinned 内存；④分配后可选 `cudaMemsetAsync` 清零；⑤把 `tensorflow::Tensor` 登记进 `pointer_mapping_` 后返回裸指针。这正好印证 u2-l2 说的「TF/TH 后端委托给框架 allocator」。

`free` 的实现——只是从账本里擦除：

[allocator.h:L348-L355](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L348-L355) —— 注意它**没有显式 `cudaFree`**，只是 `pointer_mapping_->erase(address)`。真正的显存回收发生在 `tensorflow::Tensor` 对象析构时（由 TF 的分配器池管理）。这正是 u2-l2 提到的「框架后端把生命周期交给框架」。

回到 `BertOp`，看它如何实例化这个 allocator 并喂给模型：

[BertOp.cc:L124-L130](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L124-L130) —— `ft::Allocator<ft::AllocatorType::TF> allocator(context, stream)` 一行就接好了 TF 的内存池。随后这个 `allocator` 的地址被传给 `cublasMMWrapper`（作 GEMM 工作区）和 `ft::Bert`（作 layer workspace），全程 FT 不直接调 `cudaMalloc`。

#### 4.3.4 代码实践

**实践目标**：验证 FT TF OP 的工作区显存确实由 TF 而非 FT 自己分配。

**操作步骤**：

1. 打开 `allocator.h` 的 `Allocator<AllocatorType::TF>::malloc`（第 318–346 行）。
2. 与 u2-l2 讲过的 CUDA 后端对比：CUDA 后端调 `cudaMalloc`（或 `cudaMallocAsync`），TF 后端调 `context_->allocate_temp`。
3. 在 `BertOp.cc` 全文搜索 `cudaMalloc`，确认它一次都没出现。

**需要观察的现象**：

- `BertOp.cc` 里没有任何 `cudaMalloc` / `cudaFree`。
- 所有显存要么来自 TF 输入张量（`convert_tensor`），要么来自 `Allocator<TF>`（即 `allocate_temp`）。

**预期结果**：FT 的 TF OP 在显存管理上是「全托管」的——既不复制输入，也不自建临时显存，完全融入 TF 的内存图。这是它能和 TF 的其它算子（如 XLA、内存复用优化）和平共处的前提。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Allocator<AllocatorType::TF>::malloc` 要把大小对齐到 32 的倍数？

**参考答案**：一方面是 GPU 显存对齐的通用要求（很多 kernel 要求 16/32 字节对齐）；另一方面，FT 的 INT8 路径要求 COL32 布局（承接 u9-l1），临时缓冲按 32 对齐能减少边界处理。对齐到 32 是一个保守且通用的选择。

**练习 2**：`free` 方法不调 `cudaFree`，会不会造成显存泄漏？

**参考答案**：不会。`free` 只是从 `pointer_mapping_` 擦除条目，对应的 `tensorflow::Tensor` 对象在擦除后引用计数归零、触发析构，由 TF 的 GPU 分配器（BFC allocator 等）把显存回收到池中。真正的释放由 TF 负责，FT 只负责「登记/注销」。

---

### 4.4 tf_op 目录组织与接口演进

#### 4.4.1 概念说明

了解单个 OP 之后，本节俯瞰整个 `tf_op/` 目录的组织方式，并指出一个重要现象：**FT 的模型 forward 接口经历过一次演进**，从早期的 `std::vector<Tensor>` 迁移到后期的 `TensorMap`（按名字索引）。这导致 tf_op 里的老 OP（Bert/Encoder/Decoder）和新 OP（Gpt/Decoding）用了两套不同的张量组织方式。

#### 4.4.2 核心流程

`tf_op/` 的目录结构：

```
tf_op/
├── BaseOp.h              # 公共基类
├── CMakeLists.txt        # 按 BUILD_TF / BUILD_TF2 分组编译
├── bert/                 # BertOp, BertINT8Op, weight_quantize_op  （BUILD_TF）
├── encoder/              # EncoderOp                              （BUILD_TF）
├── decoder/              # DecoderOp, FusedSelfAttentionOp        （BUILD_TF）
├── decoding/             # DecodingOp                             （BUILD_TF）
├── gpt/                  # GptOp                                  （BUILD_TF）
├── t5/                   # T5EncoderOp, T5DecodingOp              （BUILD_TF2）
└── deberta/              # DebertaOp                              （BUILD_TF2）
```

每个模型子目录都有一份 `CMakeLists.txt`，把对应的 `*Op.cc` 编译成一个独立的 `.so`（如 `tf_bert.so`、`tf_gpt.so`）。Python 端用 `tf.load_op_library` 按需加载。

接口演进体现在 `forward` 的参数类型上：

| OP | 输入输出容器 | 代表 |
|----|------------|------|
| BertOp / EncoderOp / DecoderOp | `std::vector<ft::Tensor>` | 旧式，按位置索引 |
| GptOp | `std::unordered_map<std::string, ft::Tensor>` | 新式，按名字索引 |
| DecodingOp | `ft::TensorMap` | 新式（即 unordered_map 的别名封装） |

#### 4.4.3 源码精读

先看顶层 `tf_op/CMakeLists.txt` 如何分组：

[tf_op/CMakeLists.txt:L31-L42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/CMakeLists.txt#L31-L42) —— `BUILD_TF` 开启时编译 `bert / encoder / decoder / decoding / gpt` 五个目录，`BUILD_TF2` 开启时编译 `t5 / deberta` 两个目录。注意 t5 与 deberta 只在 TF2 模式下编译，这是因为它们的依赖（如某些 TF2 特性）只在 TF2 下可用。另外文件开头 `add_definitions(-DGOOGLE_CUDA=1)` 是让 TF 头文件知道「我们在 GPU 模式编译」，前述 `#ifdef GOOGLE_CUDA` 的 kernel 注册就靠它生效。

接着看每个模型的 `.so` 如何链接。以 bert 为例：

[bert/CMakeLists.txt:L15-L16](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/CMakeLists.txt#L15-L16) —— `add_library(tf_bert SHARED ...)` 产出一个共享库 `tf_bert.so`，链接三部分：①FT 的模型库（`Bert BertINT8`）；②TF 框架库 `${tf_link}`（即 `libtensorflow_framework`）；③CUDA 库 `-lcublas -lcublasLt -lcudart`。gpt 目录与之同构，只链接 `ParallelGpt`：

[gpt/CMakeLists.txt:L15-L16](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/gpt/CMakeLists.txt#L15-L16) —— 注意 gpt 没有单独链接 `cublasAlgoMap` 目标（bert 链接了），因为它的算法表在 OP 内部用 `cublasAlgoMap` 类，相关符号已随 `ParallelGpt` 库带入。

再看接口演进。`BertOp` 用 `std::vector<ft::Tensor>`：

[BertOp.cc:L216-L223](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/bert/BertOp.cc#L216-L223) —— 输入是 `{from_tensor, sequence_length}` 两个张量，按下标 0/1 访问；输出是单个隐状态张量。位置即语义，简单但不利于扩展。

而 `GptOp` 用 `std::unordered_map<std::string, ft::Tensor>`：

[GptOp.cc:L304-L356](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/gpt/GptOp.cc#L304-L356) —— 这里用字符串键组织张量：`"input_ids"`、`"input_lengths"`、`"runtime_top_k"`、`"temperature"` 等。这种「按名字」的方式让 GPT 的众多运行期采样参数（top_k/top_p/temperature/len_penalty/repetition_penalty，承接 u8-l1 的 runtime_arg）能灵活插入：`if (top_k_ != 0) input_tensors.insert({"runtime_top_k", ...})`，存在才传，不存在则模型用默认值。这是 `TensorMap` 接口相对 `vector` 的核心优势。

`DecodingOp` 进一步把 `unordered_map` 封装成 `ft::TensorMap` 类型：

[DecodingOp.cc:L362](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/decoding/DecodingOp.cc#L362) —— 直接用 `ft::TensorMap input_tensors({...})` 构造，与 GptOp 的裸 `unordered_map` 等价，但语义更清晰。这印证 u2-l1 所说：`TensorMap` 就是「按名字索引的统一 forward 接口载体」。

#### 4.4.4 代码实践

**实践目标**：盘点 `tf_op/` 下所有模型 OP，并区分新旧接口。

**操作步骤**：

1. 列出 `tf_op/` 下所有 `*Op.cc` 文件。
2. 对每个文件，搜索 `std::vector<ft::Tensor>` 与 `ft::TensorMap`（或 `unordered_map<std::string, ft::Tensor>`），判断它用旧接口还是新接口。
3. 整理成一张表。

**需要观察的现象**：

- BertOp / EncoderOp / DecoderOp → `vector`（旧）。
- GptOp / DecodingOp → `map`（新）。

**预期结果**：能画出一张「OP → 接口类型 → 输入张量名/个数」的对照表。这张表也是后续排查「为什么某个参数传不进去」的速查手册——例如想给 BERT 加运行期参数会比较麻烦（位置式接口），而给 GPT 加则只需 `insert` 一个键值对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 t5 和 deberta 只在 `BUILD_TF2` 下编译，而 bert/gpt 在 `BUILD_TF` 下？

**参考答案**：t5 和 deberta 是较晚加入的模型，它们的 TF OP 实现依赖 TF2 的某些特性（或只在 TF2 环境下验证过）。而 bert/encoder/decoder/decoding/gpt 是早期模型，在 TF1 下开发验证。`BUILD_TF`（TF1，旧 ABI）与 `BUILD_TF2`（TF2，新 ABI）的区分见 4.5。

**练习 2**：`GptOp` 用 `unordered_map` 而非 `vector` 带来的具体好处是什么？举一个代码里的例子。

**参考答案**：好处是「可选参数」可以用 `insert` 按需添加。例如 [GptOp.cc:L316-L323](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/gpt/GptOp.cc#L316-L323)：只有当 `top_p_ != 0` 时才插入 `"runtime_top_p"`，只有当 `top_k_ != 0` 时才插入 `"runtime_top_k"`。模型在 forward 里按名字查找，找不到就用默认值。若用 `vector`，每个可选参数都得占一个固定位置，传递「不使用」还要用特殊哨兵值，既易错又难扩展。

---

### 4.5 编译与链接：BUILD_TF / BUILD_TF2 / TF_PATH 与 ABI

#### 4.5.1 概念说明

FT 默认不编译 TF OP（`BUILD_TF` / `BUILD_TF2` 默认 OFF），因为绝大多数用户用 C++ 或 PyTorch。要启用 TF 集成，必须同时满足：

1. 指明 TensorFlow 的安装路径（`TF_PATH`）。
2. 选择 TF1（`BUILD_TF`）或 TF2（`BUILD_TF2`）模式。
3. 关键：TF1 和 TF2 编译时用的 **C++ ABI 不同**，必须用对应的宏对齐，否则 `.so` 加载时会报「undefined symbol」。

#### 4.5.2 核心流程

编译 TF OP 的决策链：

```
cmake 阶段：
  BUILD_TF=ON 或 BUILD_TF2=ON ?
    否 → 不编译 tf_op
    是 → 检查 TF_PATH 是否设置
           未设置 → message(FATAL_ERROR "TF_PATH must be set ...")
           已设置 →
             加入 ${TF_PATH}/include 到头文件路径
             加入 ${TF_PATH} 到库路径
             BUILD_TF  → add_definitions(-D_GLIBCXX_USE_CXX11_ABI=0)   # 旧 ABI
             BUILD_TF2 → add_definitions(-D_GLIBCXX_USE_CXX11_ABI=1)   # 新 ABI
             进入 tf_op/CMakeLists.txt，按模式编译对应子目录
             探测 libtensorflow_framework.so / .so.1 / .so.2 得到 ${tf_link}
             每个模型 → tf_<model>.so
```

ABI 问题的本质：TF1 的 pip 包用旧版 libstdc++ ABI（`_GLIBCXX_USE_CXX11_ABI=0`）编译，TF2 用新版（`=1`）。如果 FT 用错的 ABI 编译，链接 `libtensorflow_framework` 时符号就对不上，运行时 `tf.load_op_library` 会失败。

#### 4.5.3 源码精读

先看顶层 `CMakeLists.txt` 的选项声明与路径检查：

[CMakeLists.txt:L47-L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L47-L49) —— `BUILD_TF` / `BUILD_TF2` / `BUILD_PYT` 三个框架开关都默认 OFF，三者互斥使用（实际只应开一个）。

[CMakeLists.txt:L108-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L108-L113) —— `TF_PATH` 是个 cache 字符串变量，默认空；若开了 TF 模式却没设 `TF_PATH`，直接 `FATAL_ERROR`。这就是本讲实践任务里 cmake 必须带 `-DTF_PATH=...` 的根源。

再看 ABI 宏的注入：

[CMakeLists.txt:L229-L239](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L229-L239) —— `BUILD_TF` 模式注入 `-D_GLIBCXX_USE_CXX11_ABI=0`（旧 ABI，对应 TF1 的 NGC 镜像如 `22.09-tf1-py3`），`BUILD_TF2` 模式注入 `-D_GLIBCXX_USE_CXX11_ABI=1`（新 ABI，对应 TF2）。两者都把 `${TF_PATH}/include` 加进头文件、`${TF_PATH}` 加进库搜索路径。对比 u1-l2：`BUILD_PYT` 是自动探测 PyTorch 的 ABI，而 TF 是手动二选一。

然后是 `tf_op/CMakeLists.txt` 探测 TF 框架库：

[tf_op/CMakeLists.txt:L17-L29](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/CMakeLists.txt#L17-L29) —— 依次探测 `libtensorflow_framework.so`、`.so.1`、`.so.2` 三种文件名，命中哪个就用哪个作为 `${tf_link}`（分别是 `-ltensorflow_framework`、`-l:libtensorflow_framework.so.1`、`-l:libtensorflow_framework.so.2`）。`.so.1` / `.so.2` 的后缀正好对应 TF1 / TF2 的 soname，所以 `BUILD_TF` 一般会命中 `.so.1`、`BUILD_TF2` 命中 `.so.2`。

最后，docs 给出的真实编译命令（以 TF1 为例）：

[docs/bert_guide.md:L205-L210](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L205-L210) —— 这是官方文档里的标准命令：

```bash
cmake -DSM=xx -DCMAKE_BUILD_TYPE=Release -DBUILD_TF=ON -DTF_PATH=/usr/local/lib/python3.8/dist-packages/tensorflow_core/ ..
make -j12
```

其中 `TF_PATH` 指向 NGC TF1 镜像里 `tensorflow_core` 的安装目录（其下有 `include/` 和 `libtensorflow_framework.so.1`）。

#### 4.5.4 代码实践

**实践目标**：写出编译 FT 的 TF OP 所需的完整 cmake 命令，并解释每个 `-D` 参数。

**操作步骤**：

1. 假设使用 NGC 镜像 `nvcr.io/nvidia/tensorflow:22.09-tf1-py3`，TensorFlow 装在 `/usr/local/lib/python3.8/dist-packages/tensorflow_core/`。
2. 目标 GPU 架构代号为 `xx`（如 A100 填 `80`、T4 填 `75`）。
3. 参考 [docs/bert_guide.md:L205-L210](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L205-L210) 写出命令。

**需要观察的现象 / 预期结果**（命令与逐参数解释）：

```bash
mkdir -p build && cd build
cmake -DSM=80 -DCMAKE_BUILD_TYPE=Release -DBUILD_TF=ON -DTF_PATH=/usr/local/lib/python3.8/dist-packages/tensorflow_core/ ..
make -j12
```

参数含义：

| 参数 | 含义 |
|------|------|
| `-DSM=80` | 目标 GPU 架构（A100 = sm_80）。承接 u1-l2：SM 决定为哪些架构生成 PTX/SASS。 |
| `-DCMAKE_BUILD_TYPE=Release` | Release 构建，开优化。注意这会定义 `NDEBUG`，影响日志级别（承接 u1-l5）。 |
| `-DBUILD_TF=ON` | 开启 TensorFlow（TF1）模式，触发编译 `bert/encoder/decoder/decoding/gpt` 五个 OP 目录，并注入旧 ABI 宏。 |
| `-DTF_PATH=...` | TensorFlow 安装路径，cmake 会从这里找 `include/` 和 `libtensorflow_framework.so.1`。 |

编译成功后，`build/lib/` 下会出现 `libtf_bert.so`、`libtf_gpt.so` 等共享库。在 Python 里用 `tf.load_op_library("libtf_bert.so")` 加载即可。

**已知限制**（来自 README）：FT 无法在 TensorFlow 2.10 上编译，原因是 undefined symbol 问题（见 README 的 Known issues）。实际使用时应遵循 docs 推荐的 NGC 镜像版本。此结论**待本地验证**（具体可用的 TF 版本以你本地环境为准）。

#### 4.5.5 小练习与答案

**练习 1**：如果忘了设 `-DTF_PATH`，cmake 会发生什么？

**参考答案**：cmake 会在配置阶段直接 `message(FATAL_ERROR "TF_PATH must be set if BUILD_TF or BUILD_TF2 (=TensorFlow mode) is on.")`（[CMakeLists.txt:L111-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L111-L113)），配置中断，不会进入编译。这是 FT 的硬约束。

**练习 2**：用 `BUILD_TF`（TF1）编译出的 `.so` 能否直接拿到 TF2 环境里 `load_op_library`？为什么？

**参考答案**：通常不行。`BUILD_TF` 注入的是旧 ABI（`_GLIBCCXX_USE_CXX11_ABI=0`），而 TF2 用新 ABI（`=1`）。ABI 不一致会导致 `std::string`、`std::vector` 等 STL 类型的内存布局不同，链接 `libtensorflow_framework.so.2` 时出现 undefined symbol 或运行时崩溃。必须用 `BUILD_TF2` 重新编译。

**练习 3**：为什么 `tf_op/CMakeLists.txt` 要探测 `.so`、`.so.1`、`.so.2` 三种文件名？

**参考答案**：不同版本/打包方式的 TensorFlow，其框架库的 soname 后缀不同：TF1 的 pip 包通常是 `libtensorflow_framework.so.1`，TF2 是 `.so.2`，某些源码编译版本是裸 `.so`。探测三种文件名能让 FT 适配尽可能多的 TF 安装方式，而不强迫用户手动指定链接库名。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「画 BertOp 的完整调用链」的任务。

**任务**：假设你已经在 NGC TF1 镜像里用 `-DBUILD_TF=ON -DTF_PATH=...` 编译出了 `libtf_bert.so`，并在 Python 里 `tf.load_op_library` 加载后调用了 `bert(from_tensor, to_tensor, seq_len, ...各层权重..., head_num=12, size_per_head=64, ...)`。请画出从 Python 调用到 `ft::Bert::forward` 执行的完整时序，并标注每一步发生在哪个源码位置。

**参考答案（时序图）**：

```
Python: module.bert(...)
  │  TF graph 执行到 Bert 节点
  ▼
[构造期，仅首次] BertOp 构造函数 (BertOp.cc:86-101)
  │  BaseOp 构造: 建 cublas/cublasLt handle + mutex (BaseOp.h:47-57)
  │  GetAttr 读 head_num/size_per_head/... 
  │  getSMVersion() + new cublasAlgoMap("gemm_config.in")
  ▼
[每次调用] BertOp::Compute (BertOp.cc:108-236)
  │
  ├─① 校验输入数 == num_layer*16+3 (BertOp.cc:110-112)
  ├─② 读 batch_size / from_seq_len (BertOp.cc:114-115)
  ├─③ 取 stream + cublasSetStream (BertOp.cc:121-123)
  ├─④ 建 Allocator<AllocatorType::TF> (BertOp.cc:124)
  │      └─ 之后 FT 的临时显存走 context->allocate_temp (allocator.h:318-346)
  ├─⑤ 建 cublasMMWrapper + setFP16GemmConfig (BertOp.cc:125-142)
  ├─⑥ for 每层: get_tensor 把权重指针挂进 BertWeight (BertOp.cc:147-187)
  │      └─ 零拷贝: 直接复用 TF 张量指针 (BaseOp.h:67-85)
  ├─⑦ getAttentionType 推导 FUSED/UNFUSED (BertOp.cc:191-192)
  ├─⑧ 构造 ft::Bert 模型对象 (BertOp.cc:194-209)
  ├─⑨ allocate_output + convert_tensor (BertOp.cc:211-223)
  │      └─ 输入零拷贝转 ft::Tensor (BaseOp.h:99-121)
  └─⑩ bert.forward(&output_tensors, &input_tensors, &bert_weights) (BertOp.cc:226)
         └─ 进入 FT 核心引擎（u4-l1 BERT forward 主流程）
```

**验证要点**：

1. 显存零拷贝：输入与权重全程不复制，只是指针绑定。
2. 临时显存托管：FT 工作区由 TF 的 `allocate_temp` 提供。
3. handle 复用：cuBLAS handle 在 OP 实例生命周期内复用，不每次重建。
4. 接口形态：BertOp 用 `vector<Tensor>`（旧式），与 GptOp 的 `TensorMap`（新式）形成对照。

完成此图后，你应该能独立读懂 `EncoderOp`、`GptOp`、`DecodingOp` 等任何 FT TF OP——它们的骨架完全一致，差异只在权重布局、接口容器和具体模型类。

## 6. 本讲小结

- FT 把每个模型封装成一个独立的 TensorFlow 自定义 OP，编译成 `tf_<model>.so`，由 Python 端 `tf.load_op_library` 加载。
- `BaseOp<T>` 是所有 TF OP 的抽象基类，封装 cuBLAS/cuBLASLt handle 的创建销毁与互斥锁，并提供 `convert_tensor` / `get_tensor` 等 TF→FT 张量转换方法。
- 一个 TF OP 的完整三件套是：`REGISTER_OP`（声明签名）→ `OpKernel` 子类（实现 `Compute`）→ `REGISTER_KERNEL_BUILDER`（注册 GPU kernel）；`TFTraits` 负责 TF 的 `Eigen::half` 到 FT 的 `half` 类型翻译。
- `Compute` 的标准流程是：取 stream → 建 `Allocator<TF>` 与 `cublasMMWrapper` → 装权重 → 构模型 → `convert_tensor` → `forward`；其中 `Allocator<AllocatorType::TF>` 把 FT 的工作区显存委托给 TF 的 `context->allocate_temp`，全程零 `cudaMalloc`。
- 接口演进：Bert/Encoder/Decoder 用旧式 `std::vector<Tensor>`，Gpt/Decoding 用新式 `TensorMap`（按名字索引），后者更利于可选运行期参数。
- 编译开关：`BUILD_TF`（TF1，旧 ABI `_GLIBCXX_USE_CXX11_ABI=0`）与 `BUILD_TF2`（TF2，新 ABI）二选一，必须配 `TF_PATH`，否则 cmake 直接报错；两者分别编译不同子目录（TF1→bert/encoder/decoder/decoding/gpt，TF2→t5/deberta）。

## 7. 下一步学习建议

- **下一讲 u10-l3**：Triton backend 部署。Triton backend 与 tf_op 解决同一类问题（把 FT 暴露给上层框架），但它把模型对象缓存进 `ModelInstance`、用线程池服务并发请求，比 TF OP 的「每次 Compute 重建模型」更适合生产部署。学完可对比两者在生命周期管理与并发模型上的取舍。
- **延伸阅读**：
  - [src/fastertransformer/tf_op/gpt/GptOp.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/tf_op/gpt/GptOp.cc) 完整读一遍，体会 `TensorMap` 接口如何承载 GPT 的众多运行期采样参数（承接 u8-l1）。
  - 对照 u10-l1 的 `th_op`（PyTorch）：两者的 `convert_tensor` 思想完全一致（零拷贝指针绑定），只是张量来源一个是 `tf::Tensor`、一个是 `torch::Tensor`。
  - 阅读 `examples/tensorflow/bert/bert_example.py`（如果存在），看 Python 端如何组织权重并调用 `module.bert(...)`，把本讲的 C++ 侧与 Python 侧闭环。
