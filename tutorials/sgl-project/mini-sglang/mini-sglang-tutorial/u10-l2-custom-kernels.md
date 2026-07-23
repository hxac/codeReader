# 自定义 Kernel（CUDA/Triton/tvm-ffi）

## 1. 本讲目标

学完本讲，读者应该能够：

- 说出 `python/minisgl/kernel/` 包的整体职责：用 **tvm-ffi** 把 C++/CUDA 源码即时编译（JIT）或一次性构建（AOT）成可在 Python 里直接调用的模块。
- 区分两条加载路径：`load_jit`（带 C++ 模板参数、按 dtype/尺寸特化、懒编译并缓存）与 `load_aot`（无参数、构建一次即可）。
- 讲清 `indexing`、`store_cache`、`fast_compare_key` 三个 kernel 各自做什么、被上层哪个模块调用。
- 理解 `PyNCCLCommunicator` 如何初始化 NCCL 通信器、为什么需要一个「最大缓冲」（对称内存窗口）、以及它与 CUDA Graph 回放的关系。
- 知道 `kernel/__main__.py` 的真实作用（生成 `.clangd` 配置，辅助 IDE 编辑 C++/CUDA 源码），并知道 kernel 的正确性自测其实放在 `tests/kernel/`。

## 2. 前置知识

本讲是专家层，需要读者已经具备以下基础（对应前置讲义）：

- **KV Cache 池存储**（[u6-l1](u6-l1-kvcache-pool-prefix-abstract.md)）：`MHAKVCache` 用一块 `(2, layers, pages, page_size, local_kv_heads, head_dim)` 张量持有所有层的 K/V，新算出的 K/V 要按槽位下标 `out_loc` 写进这块池子。本讲的 `store_cache` 就是那个「写入」动作的底层 GPU 实现。
- **张量并行与集合通信**（[u9-l1](u9-l1-linear-tp-distributed.md)）：TP 下行并行 Linear 之后要 `all_reduce` 求和、词表并行的 lm_head 要 `all_gather` 拼接；`DistributedCommunicator` 用插件栈选择通信后端。本讲的 `PyNCCLCommunicator` 就是栈顶那个可替换的自研 NCCL 实现。
- **词表并行 embedding**（[u9-l2](u9-l2-embedding-norm-rope-attention.md)）：每张卡只持有词表的一段（`vocab_range`），查询时越界的 token 要置零、再 `all_reduce` 复原。本讲 `indexing` 的 `vocab_range`（masked）模式正是为这个场景准备的。

此外需要一点 CUDA 直觉：

- **warp**：GPU 上 32 个线程组成的执行单元，是本讲 kernel 做内存搬运的最小单位。一个 warp 协同搬运一段连续字节（128 字节对齐）效率最高。
- **C++ 模板特化（template specialization）**：模板参数在编译期确定，把「每行多少字节」「分几路 warp」等常量编进 kernel，编译器才能充分展开循环、把内存搬运指令固化下来。
- **NCCL 与对称内存**：NCCL 是 NVIDIA 的多卡集合通信库。`ncclMemAlloc` + `ncclCommWindowRegister` 注册的「对称内存（symmetric memory）」是 NCCL 支持被 CUDA Graph 录制的前提。

> 术语澄清：本讲标题里的「Triton」指的是同包下的 `kernel/triton/fused_moe.py`（MoE 的 Triton kernel，已在 [u10-l1](u10-l1-moe-fused.md) 讲过）。本讲聚焦另外三类 kernel：CUDA/C++ kernel 与 PyNCCL 通信器，不再重复 Triton MoE。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [kernel/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/__init__.py) | 包入口，统一导出 `indexing`/`store_cache`/`fast_compare_key`/`init_pynccl`/`PyNCCLCommunicator` 等。 |
| [kernel/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py) | 加载基础设施：`load_jit`/`load_aot`/`make_cpp_args`/`KernelConfig`，定义编译选项与模板参数序列化。 |
| [kernel/index.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py) | `indexing`：词表并行 embedding 取行的 CUDA kernel 的 Python 封装。 |
| [kernel/store.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/store.py) | `store_cache`：把新算的 K/V 按 `out_loc` 写进 KV pool 的 CUDA kernel 封装。 |
| [kernel/radix.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/radix.py) | `fast_compare_key`：CPU 上逐元素比较两个 1D int 张量、返回首个不同位置。 |
| [kernel/pynccl.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py) | `init_pynccl`：构造 `PyNCCLCommunicator`（NCCL 通信器 + 对称内存窗口）。 |
| [kernel/__main__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/__main__.py) | `python -m minisgl.kernel` 入口，生成 `.clangd` 配置辅助 IDE 编辑 C++/CUDA 源。 |
| [csrc/jit/index.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/index.cu) | `indexing` 的 CUDA 实现（`IndexKernel`）。 |
| [csrc/jit/store.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/store.cu) | `store_cache` 的 CUDA 实现（`StoreKernel`）。 |
| [csrc/src/radix.cpp](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp) | `fast_compare_key` 的 C++ 实现。 |
| [csrc/src/pynccl.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu) | `NCCLWrapper`（通信器对象）的 C++/CUDA 实现。 |
| [tests/kernel/](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel) | `test_index.py`/`test_store.py`/`test_comm.py`/`test_tensor.py`：各 kernel 的正确性与性能基准。 |

源码目录约定（见 `utils.py` 第 9–13 行）：`.cu`/`.cpp` 源码位于 `kernel/csrc/` 下，其中 `csrc/jit/` 放「带模板参数、被 JIT 内联编译」的源（index、store），`csrc/src/` 放「构建一次、无参数」的源（radix、pynccl、tensor），`csrc/include/minisgl/` 放公共头（`utils.h`/`utils.cuh`/`warp.cuh`/`tensor.h`/`nccl227.h`）。

## 4. 核心概念与源码讲解

### 4.1 kernel 包的整体架构与 tvm-ffi 加载机制

#### 4.1.1 概念说明

Mini-SGLang 不直接写「裸 Python + torch 自带算子」，而是把性能关键路径下沉到自己写的 C++/CUDA kernel 里。这些源码不能被 Python 直接 `import`，需要先编译成动态库、再通过 **tvm-ffi**（Apache TVM 的 FFI 框架，项目依赖 `apache-tvm-ffi`）暴露成可调用的 Python 对象。

`kernel/utils.py` 提供两条统一的加载入口，区别在于「kernel 是否依赖运行时才知道的常量」：

- **`load_jit`（即时编译）**：kernel 用 C++ 模板参数特化，例如「每行 1024 字节、分 2 路 warp」。这些参数要到运行时（拿到真实 dtype 和形状后）才能确定，所以首次调用时按参数即时编译、之后用 `@functools.cache` 缓存编译产物。`indexing`、`store_cache` 走这条。
- **`load_aot`（提前/一次性构建）**：kernel 不带运行时模板参数，构建一次即可复用。`fast_compare_key`、`PyNCCLCommunicator`、`test_tensor` 走这条。

> 注意：这里的 JIT/AOT 都是「懒加载」——第一次调用 Python 封装函数时才触发编译，不是 `import minisgl` 时全量编译。两者真正的区别是「是否按模板参数特化并缓存多份」。

#### 4.1.2 核心流程

一个 kernel 从源码到被调用，经历以下流水线：

```
Python 封装函数(如 indexing)
   │  首次调用
   ▼
@functools.cache 的 _jit_xxx_module(element_size, ...)
   │  把模板参数序列化成 C++ 字面量
   ▼
make_cpp_args(1024, 2, 128, 1, False)  →  CppArgList["1024","2","128","1","false"]
   │  str() 拼成 "1024, 2, 128, 1, false"
   ▼
load_jit("index", *args, cuda_wrappers=[("launch", "IndexKernel<1024, 2, 128, 1, false>::run")])
   │  生成 #include "index.cu" + TVM_FFI_DLL_EXPORT_TYPED_FUNC(launch, (IndexKernel<...>::run))
   ▼
tvm_ffi.cpp.load_inline(...)  →  编译为扩展名为 minisgl__index_1024_2_128_1_false 的动态库
   │  缓存到磁盘（tvm-ffi 内置 cache）
   ▼
module.launch(weights, indices, output, vocab_range)  ←  之后调用直接走这里，零编译开销
```

关键点：模板参数被同时写进 **(a) 模板实参** `IndexKernel<...>` 和 **(b) 模块名** `minisgl__index_1024_2_...`。后者是 tvm-ffi 的缓存键——不同的 `element_size`/`num_splits` 会得到不同的模块名，因而各自独立编译、互不覆盖。

#### 4.1.3 源码精读

先看两条加载入口的核心区别（[kernel/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py)）：

`load_aot` 直接把源文件交给 `tvm_ffi.cpp.load` 编译（[utils.py:L53-L84](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L53-L84)）——源文件本身已经写好了要导出的符号（如 radix.cpp 末尾的 `TVM_FFI_DLL_EXPORT_TYPED_FUNC(fast_compare_key, fast_compare_key)`）。

`load_jit` 则把源文件 `#include` 进一段「内联源码」，并额外注入 wrapper 宏（[utils.py:L87-L129](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L87-L129)）：

```python
cpp_sources = [f'#include "{path}"' for path in cpp_paths]
cpp_sources += [_make_wrapper(tup) for tup in cuda_wrappers]
return load_inline(_make_name(*args), cpp_sources=..., cuda_sources=..., ...)
```

其中 `_make_wrapper` 把 `("launch", "IndexKernel<...>::run")` 拼成导出宏（[utils.py:L37-L39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L37-L39)）：

```python
def _make_wrapper(tup):
    export_name, kernel_name = tup
    return f"TVM_FFI_DLL_EXPORT_TYPED_FUNC({export_name}, ({kernel_name}));"
```

即 `TVM_FFI_DLL_EXPORT_TYPED_FUNC(launch, (IndexKernel<1024, 2, 128, 1, false>::run))`，它导出一个名为 `launch` 的 C 函数，内部调用模板实例化的 `IndexKernel<...>::run`。Python 侧随后用 `module.launch(...)` 调用它。

模板参数本身由 `make_cpp_args` 把 Python 值转成 C++ 字面量（`True→"true"`、`False→"false"`、数字直传），见 [utils.py:L42-L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L42-L50)；而 `KernelConfig` 把 `(num_threads, max_occupancy, use_pdl)` 序列化成模板串，见 [utils.py:L22-L30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L22-L30)。

包入口统一导出这些函数，外部一律 `from minisgl.kernel import xxx`（[kernel/__init__.py:L1-L17](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/__init__.py#L1-L17)）。

#### 4.1.4 代码实践

**实践目标**：观察 JIT 的「首次调用触发编译、后续零开销」行为。

**操作步骤**：

1. 打开 [tests/kernel/test_tensor.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_tensor.py)，它是整个 tvm-ffi 加载流水线的最小烟雾测试（AOT 路径，无需 GPU 计算正确性，只验证能编译并通过形状/_dtype 校验）。
2. 在有 GPU 与已装好 `apache-tvm-ffi` 的环境里运行：`python -m tests.kernel.test_tensor`（若环境无 GPU，改为阅读 `tests/kernel/test_index.py` 的断言理解行为）。
3. 注意首次运行会有一段明显的「编译等待」（nvcc 编译 `.cu`），再次运行则瞬间完成——这就是磁盘缓存生效。

**需要观察的现象**：首次运行日志里能看到 tvm-ffi 的编译输出；第二次运行几乎无编译耗时。

**预期结果**：测试无报错通过。若运行失败，多半是 `apache-tvm-ffi` 未安装或 CUDA toolkit 缺失——这是 kernel 包的硬依赖。

> 待本地验证：编译耗时取决于机器，本讲无法给出确切数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `indexing` 用 `load_jit` 而 `fast_compare_key` 用 `load_aot`？

**参考答案**：`indexing` 的 kernel 行为依赖运行时才知道的 `element_size`（= embedding_dim × dtype 字节数）和 `num_splits`，需要按这些常量做 C++ 模板特化才能高效，所以走 JIT、按参数缓存多份编译产物；`fast_compare_key` 只是逐元素比较，不依赖任何运行时常量做特化，构建一次即可，所以走 AOT。

**练习 2**：`_make_name("index", 1024, 2, 128, 1, False)` 的返回值是什么？它为什么重要？

**参考答案**：返回 `"minisgl__index_1024_2_128_1_false"`（见 [utils.py:L33-L34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/utils.py#L33-L34)）。它是 tvm-ffi 的动态库/缓存键名——不同特化参数得到不同名字，确保各种 dtype/尺寸的 kernel 各自独立编译、互不覆盖，也使磁盘缓存能正确命中。

---

### 4.2 indexing：词表并行 embedding 取行

#### 4.2.1 概念说明

`indexing` 解决一个非常具体的问题：给定一个权重矩阵 `weights`（形状 `(vocab, embed_dim)`，每行是一个 token 的 embedding 向量）和一组下标 `indices`（一批 token id），把对应行**整行搬运**到输出张量里。

这其实就是 `torch.nn.functional.embedding` 的功能，那为什么要自己写 kernel？两个原因：

1. **性能**：embedding 查表是纯内存搬运（把权重某行拷到输出），自己写的 warp 级 kernel 能让一个 warp 协同搬运一整行、把拷贝指令固化，比 PyTorch 通用路径更快（`tests/kernel/test_index.py` 用 `F.embedding` 做基线对比）。
2. **词表并行（TP）**：每张卡只持有词表的一段（`vocab_range`），查询时**越界**（不属于本卡的）token 要被置零，之后再用 `all_reduce` 把各卡结果加起来复原完整 embedding。`indexing` 的 `vocab_range` 参数（masked 模式）正是干这个。

#### 4.2.2 核心流程

`indexing` 的 Python 封装先决定「一行要多大、分几路 warp 来搬」（[kernel/index.py:L41-L47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py#L41-L47)）：

```
element_size = embed_dim × dtype字节数   # 一行多少字节
若 element_size % 2048 == 0  →  num_splits = 4   # 一行很大，用 4 个 warp 分担
若 element_size % 1024 == 0  →  num_splits = 2
否则                          →  num_splits = 1
```

即「行越宽、分的 warp 越多」，提升内存级并行度。随后按 `(element_size, num_splits, config)` JIT 出特化 kernel 并调用（[index.py:L48-L49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py#L48-L49)）。

CUDA 侧（[csrc/jit/index.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/index.cu)）核心循环：

```
总 warp 数 num_warps = num_splits × num_indices   # 每个 token 占 num_splits 个 warp
对每个 warp_id：
    token = indices[warp_id / num_splits]          # 本 warp 属于哪个 token
    段号  = warp_id % num_splits                    # 本 warp 搬这个 token 的第几段
    dst = output + warp_id * kSizePerWarp
    src = weight  + token*kSize + 段号*kSizePerWarp
    warp::copy<kSizePerWarp>(dst, src)             # 一个 warp 协同拷一段
```

masked 模式（`vocab_range` 非空）多一步：`pos = indices[...] - start`，若 `pos >= length`（越界）就把目标段 `warp::reset` 清零（[index.cu:L61-L96](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/index.cu#L61-L96)）。

> 数学上，词表并行的 embedding 等价于：每卡只在属于自己的词表段 `[start, start+length)` 内查表、其余置零，再 `all_reduce(SUM)`。因为每个 token id 只会在恰好一张卡上命中（非零），其余卡贡献 0，求和即复原。设 token \(t\) 的完整 embedding 为 \(e_t\)，rank \(r\) 持有段 \(V_r\)，则 \(\sum_r \mathbf{1}[t \in V_r] \cdot e_t = e_t\)。

#### 4.2.3 源码精读

Python 封装与 `num_splits` 决策（[kernel/index.py:L31-L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py#L31-L50)）：

```python
def indexing(weights, indices, *, output=None, vocab_range=None):
    if output is None:
        output = weights.new_empty(indices.shape[0], weights.shape[1])
    element_size = weights.shape[1] * weights.element_size()
    if element_size % 2048 == 0:   num_splits = 4
    elif element_size % 1024 == 0: num_splits = 2
    else:                          num_splits = 1
    module = _jit_index_module(element_size, num_splits=num_splits)
    module.launch(weights, indices, output, vocab_range)
    return output
```

关键：`element_size`、`num_splits` 被同时编进模板实参与模块名，确保不同尺寸的 embedding 各自有一份特化 kernel（`_jit_index_module` 见 [index.py:L15-L28](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py#L15-L28)）。

CUDA kernel 的搬运主体（[csrc/jit/index.cu:L31-L59](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/index.cu#L31-L59)）：

```cpp
const auto pos = indices[warp_id / kNumSplits];          // 目标 token
const auto dst = pointer::offset(output, warp_id * kSizePerWarp);
const auto src = pointer::offset(weight, pos * kSize,
                                 (warp_id % kNumSplits) * kSizePerWarp);
warp::copy<kSizePerWarp>(dst, src);                       // 一个 warp 搬一段
```

其中 `warp::copy<kBytes>`（[csrc/include/minisgl/warp.cuh:L40-L59](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/include/minisgl/warp.cuh#L40-L59)）用 `uint4`/`uint2`/`uint1` 等「内存包」让 32 个 lane 协同搬运 `kBytes` 字节，是整个 kernel 包复用的内存搬运原语。

上层调用点：[layers/embedding.py:L34-L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L34-L42)，`VocabParallelEmbedding.forward` 调 `indexing`，TP>1 时传入 `vocab_range` 并随后 `all_reduce`：

```python
y = indexing(weights=self.weight, indices=x,
             vocab_range=self.vocab_range if self.tp_size > 1 else None)
return self._comm.all_reduce(y) if self.tp_size > 1 else y
```

#### 4.2.4 代码实践

**实践目标**：验证 `indexing` 与 `F.embedding` 结果一致，并感受 masked 模式的置零行为。

**操作步骤**：

1. 阅读 [tests/kernel/test_index.py:L13-L52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_index.py#L13-L52)：`ref_indexing` 是用 `F.embedding` 写的参考实现，`test_indexing` 对比两者结果并用 `compare_memory_kernel_perf` 测吞吐。
2. 在 GPU 环境运行：`python -m tests.kernel.test_index`。
3. 再看 `test_indexing_with_mask`（[test_index.py:L64-L101](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_index.py#L64-L101)），它设 `MASK_RANGE = (MASK_LENGTH, MASK_LENGTH)`（即 `start=length`，模拟只持有一段词表）。

**需要观察的现象**：`assert torch.all(result == expected)` 通过；masked 模式下，落在 `[start, start+length)` 之外的 token，其输出行被置零。

**预期结果**：所有 `bs`（2 的幂）下断言通过；性能数字上 `indexing` 的访存带宽应优于或接近 `F.embedding` 基线。无 GPU 环境则只做源码阅读。

> 待本地验证：具体带宽数字依赖 GPU 型号。

#### 4.2.5 小练习与答案

**练习 1**：`vocab_range` 的语义是 `(start, length)` 还是 `(start, end)`？masked kernel 如何用它判断越界？

**参考答案**：是 `(start, length)`（见 `indexing` 签名注释与 [embedding.py:L28](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L28) `self.vocab_range = (start_idx, finish_idx - start_idx)`）。kernel 里 `pos = indices[...] - start`，当 `pos >= length` 即判越界并置零（[index.cu:L84-L92](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/index.cu#L84-L92)）。

**练习 2**：如果一个模型的 `embed_dim=4096`、`dtype=bfloat16`，`num_splits` 会取多少？

**参考答案**：`element_size = 4096 × 2 = 8192` 字节，`8192 % 2048 == 0`，故 `num_splits = 4`。

---

### 4.3 store_cache：把 K/V 写入分页 KV pool

#### 4.3.1 概念说明

`store_cache` 解决前向计算中「把当前层新算出的 K、V 写回 KV cache 池」这一步。回顾 u6-l1：KV cache 是一块巨大的 paged 张量 `_kv_buffer`，新算出的 K/V 张量（来自 attention 的 qkv 投影）需要按 `out_loc`（每个 token 在池子里的目标槽位下标）**分散地**写进去。

这等价于 `k_cache[indices] = k; v_cache[indices] = v`，但同样地，自定义 warp kernel 比通用 scatter 写更快、且能把每行字节数编进模板。它是每个注意力后端 `forward` 里「先 store_kv 落池、再算注意力」定序的第一步（见 u7-l1/u7-l2）。

#### 4.3.2 核心流程

Python 封装把 cache 与输入都「压平」成 `(num_tokens, flat_dim)` 的二维视图，算出每行字节数，再 JIT 特化（[kernel/store.py:L30-L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/store.py#L30-L42)）：

```
k_cache = k_cache.view(num_tokens, -1)   # 压平 heads×head_dim
element_size = flat_dim × dtype字节数
module = _jit_store_module(element_size)  # 按 element_size 特化
module.launch(k_cache, v_cache, indices, k, v)
```

CUDA 侧（[csrc/jit/store.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/store.cu)）每个 warp 负责一个 token，先写 K 再写 V：

```
对每个 warp_id（< length，length=token 数）：
    pos = indices[warp_id]                          # 目标槽位
    warp::copy<element_size>( k_cache + pos*kv_cache_stride,  k + warp_id*kv_input_stride )
    warp::copy<element_size>( v_cache + pos*kv_cache_stride,  v + warp_id*kv_input_stride )
```

`kv_cache_stride` 与 `kv_input_stride` 分别是「池里一行跨多少字节」「输入一行跨多少字节」，用来兼容 cache（paged，可能带 padding）与输入（连续）两种布局。

> 注意：该 kernel **不允许同一批里有重复 `indices`**（否则两个 warp 写同一槽位产生数据竞争）。测试 [test_store.py:L20-L21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_store.py#L20-L21) 特意用 `torch.randperm` 生成不重复下标来回避这一点；真实调度中 prefill/decode 的 `out_loc` 也保证唯一。

#### 4.3.3 源码精读

Python 封装（[kernel/store.py:L30-L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/store.py#L30-L42)）：

```python
def store_cache(k_cache, v_cache, indices, k, v):
    num_tokens = k_cache.shape[0]
    k_cache = k_cache.view(num_tokens, -1)
    v_cache = v_cache.view(num_tokens, -1)
    element_size = k_cache.shape[1] * k_cache.element_size()
    module = _jit_store_module(element_size)
    module.launch(k_cache, v_cache, indices, k, v)
```

CUDA 搬运主体（[csrc/jit/store.cu:L42-L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/store.cu#L42-L50)）：

```cpp
if (warp_id < length) {
  const auto pos = static_cast<const T *>(indices)[warp_id];
  const auto dst_k = pointer::offset(k_cache, pos * kv_cache_stride);
  const auto src_k = pointer::offset(k, warp_id * kv_input_stride);
  warp::copy<kElementSize>(dst_k, src_k);
  const auto dst_v = pointer::offset(v_cache, pos * kv_cache_stride);
  const auto src_v = pointer::offset(v, warp_id * kv_input_stride);
  warp::copy<kElementSize>(dst_v, src_v);
}
```

上层调用点：[kvcache/mha_pool.py:L45-L56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L45-L56)，`MHAKVCache.store_kv` 把某层的 buffer 按 `_storage_shape = (num_pages*page_size, local_kv_heads, head_dim)` 视图化后交给 `store_cache`：

```python
def store_kv(self, k, v, out_loc, layer_id):
    from minisgl.kernel import store_cache
    store_cache(
        k_cache=self._k_buffer[layer_id].view(self._storage_shape),
        v_cache=self._v_buffer[layer_id].view(self._storage_shape),
        indices=out_loc, k=k, v=v,
    )
```

`_storage_shape` 的定义见 [mha_pool.py:L37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L37)；`store_kv` 又被各注意力后端的 `forward` 在算注意力前调用（见 [u7-l2](u7-l2-flashinfer-backend.md)）。

#### 4.3.4 代码实践

**实践目标**：验证 `store_cache` 等价于 `k_cache[indices] = k`，并理解为何基线用 `@torch.compile`。

**操作步骤**：

1. 阅读 [tests/kernel/test_store.py:L9-L53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_store.py#L9-L53)。注意基线 `baseline()` 用 `@torch.compile()` 包裹了 `k_cache[indices] = k; v_cache[indices] = v`——这是 PyTorch 能给出的最快通用 scatter 写。
2. 在 GPU 环境运行：`python -m tests.kernel.test_store`。
3. 关注断言 `torch.all(k_cache[indices] == k)` 与 `torch.all(v_cache[indices] == v)`。

**需要观察的现象**：断言通过；`store_cache` 的带宽通常优于未编译的朴素 scatter，与 `torch.compile` 基线互有高低。

**预期结果**：所有 `bs` 下断言通过。若手动把 `indices` 改成含重复值，会观察到某些槽位被覆盖（这正是「不允许重复 indices」的来由）。无 GPU 则做源码阅读。

> 待本地验证：性能对比数字依赖硬件。

#### 4.3.5 小练习与答案

**练习 1**：`store_cache` 为什么要先 `k_cache.view(num_tokens, -1)` 压平？

**参考答案**：KV buffer 的逻辑形状是 `(num_pages, page_size, local_kv_heads, head_dim)` 等多维，但「写一个 token 的 K」只需要把它在池里那段连续内存当成「一行」整体拷贝。压平成 `(num_tokens, flat_dim)` 后，`element_size = flat_dim × dtype字节数` 就能作为模板参数特化 `warp::copy<kElementSize>`，让一个 warp 一次搬完一个 token 的全部 K（或 V）。

**练习 2**：为什么测试要强调「不能容忍重复 indices」？

**参考答案**：kernel 里每个 warp 独立写 `indices[warp_id]` 指向的槽位，若两个 warp 的 `pos` 相同，就是两个线程块写同一块显存的数据竞争，结果不确定。真实调度的 `out_loc` 保证唯一，故 kernel 不做去重防护。

---

### 4.4 fast_compare_key：radix 树前缀匹配的逐元素比较

#### 4.4.1 概念说明

`fast_compare_key` 是本讲里唯一的 **CPU** kernel（不是 GPU kernel）。它解决基数树（radix tree）匹配时的一个高频小动作：给定节点的 key（一段 token 序列）和新来的 `input_ids`，找出它们**第一个不相同的位置**——也就是公共前缀长度。

回顾 u6-l2：`RadixPrefixCache` 用基数树压缩共享前缀，匹配时要从 root 逐节点往下走，每个节点都要回答「我的 key 和 input_ids 当前段有多少前缀重合」。这个比较发生在调度线程（CPU），数据也是 CPU 上的 1D int 张量，所以没必要上 GPU，但需要一个比 Python 循环快得多的 C++ 实现。

#### 4.4.2 核心流程

Python 封装极简，加载 AOT 模块并调用导出的 `fast_compare_key`（[kernel/radix.py:L13-L20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/radix.py#L13-L20)）。C++ 侧（[csrc/src/radix.cpp:L19-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp#L19-L40)）逻辑：

```
校验：a、b 都是 1D、连续、CPU、int32/int64 张量，且 dtype 相同
common_len = min(len(a), len(b))
用 std::mismatch 找到 [0, common_len) 内第一个 a[i] != b[i] 的位置 i
返回 i   （若全部相等则返回 common_len）
```

`std::mismatch` 是 C++ 标准库的逐元素比较，编译器会向量化（SIMD）展开，远快于 Python 的 `for` 循环。

> 返回值语义：返回的是「公共前缀长度」，即首个不同元素的下标；若比较范围 `common_len` 内全等，则返回 `common_len`（受限于较短的张量）。调用方 `RadixTreeNode.get_match_len` 据此决定是否需要 `split_at` 分裂节点（见 u6-l2）。

#### 4.4.3 源码精读

Python 封装（[kernel/radix.py:L18-L20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/radix.py#L18-L20)）：

```python
def fast_compare_key(x: torch.Tensor, y: torch.Tensor) -> int:
    # compare 2 1-D int cpu tensors for equality
    return _load_radix_module().fast_compare_key(x, y)
```

C++ 实现主体（[csrc/src/radix.cpp:L19-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp#L19-L40)）：

```cpp
auto fast_compare_key(const TensorView a, const TensorView b) -> size_t {
  RuntimeCheck(_is_1d_cpu_int_tensor(a) && _is_1d_cpu_int_tensor(b));
  RuntimeCheck(a.dtype() == b.dtype());
  const auto common_len = std::min(a.size(0), b.size(0));
  // int64 / int32 两个分支，用 std::mismatch 找首个不同位置
  const auto diff_pos = std::mismatch(a_ptr, a_ptr + common_len, b_ptr);
  return static_cast<size_t>(diff_pos.first - a_ptr);
}
```

校验函数 `_is_1d_cpu_int_tensor` 强制「1D、连续、CPU、int32/int64」（[radix.cpp:L12-L17](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp#L12-L17)）；导出宏在文件末尾（[radix.cpp:L44](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp#L44)）。

上层调用点：[kvcache/radix_cache.py:L63-L67](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L63-L67)，`RadixTreeNode.get_match_len` 用它算匹配长度：

```python
def get_match_len(self, input_ids: torch.Tensor) -> int:
    from minisgl.kernel import fast_compare_key
    return fast_compare_key(self._key, input_ids)
```

#### 4.4.4 代码实践

**实践目标**：用最小例子确认 `fast_compare_key` 返回的是「公共前缀长度」。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：需在有 apache-tvm-ffi 的环境运行
import torch
from minisgl.kernel import fast_compare_key

a = torch.tensor([1, 2, 3, 4, 5], dtype=torch.int32)
b = torch.tensor([1, 2, 9, 9], dtype=torch.int32)   # 第 2 位开始不同
print(fast_compare_key(a, b))   # 预期 2（公共前缀 [1,2] 长度为 2）
print(fast_compare_key(a, a))   # 预期 5（全等，返回较短者长度 5）
```

1. 在装好依赖的 CPU 环境运行上述片段。
2. 把 `b` 改成 `dtype=torch.float32` 再调用，观察抛错。

**需要观察的现象**：第一个打印为 `2`，第二个为 `5`；改成 float 会触发 `_is_1d_cpu_int_tensor` 校验失败抛异常。

**预期结果**：符合上述断言。无依赖环境则直接阅读 [radix.cpp:L19-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/radix.cpp#L19-L40) 推理结果。

> 待本地验证：示例代码未在所有平台验证，依赖 `apache-tvm-ffi`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fast_compare_key` 走 CPU 而不是 GPU？

**参考答案**：它服务于 radix 树匹配，发生在调度线程（CPU），输入也是 CPU 上的 1D int 张量（节点的 `_key` 与 `input_ids`）。把这点小数据搬到 GPU 再搬回来，启动 kernel 的开销远大于比较本身，得不偿失；用 C++ `std::mismatch`（向量化）在 CPU 上跑已足够快。

**练习 2**：若 `a` 长 10、`b` 长 3 且 `b` 是 `a` 的前缀，返回值是多少？为什么？

**参考答案**：返回 `3`。因为 `common_len = min(10, 3) = 3`，比较范围只有前 3 个元素且全等，`std::mismatch` 走到末尾未发现差异，返回 `common_len = 3`。调用方据此知道「`b` 完全命中、需继续往下走子节点」。

---

### 4.5 PyNCCLCommunicator：自研 NCCL 通信器与最大缓冲

#### 4.5.1 概念说明

`PyNCCLCommunicator` 是 Mini-SGLang 自己包的一层 NCCL 通信器，提供 `all_reduce` / `all_gather` / `get_buffer`。回顾 u9-l1：`DistributedCommunicator` 用插件栈抽象通信后端，默认栈底是 `TorchDistributedImpl`（`torch.distributed`），而 `enable_pynccl_distributed` 会把 `PyNCCLDistributedImpl` 追加到栈顶（`plugins[-1]`），此后所有集合通信都走 PyNCCL。

为什么要再包一层？核心动机是 **CUDA Graph 兼容性**。`torch.distributed` 的 NCCL 调用在被 CUDA Graph 录制时容易出问题（内部有 host-side 状态、可能调 stream API）；而自研这一层可以把通信缓冲**预先注册成 NCCL 对称内存窗口**，地址固定、可被 graph 录制，从而让 decode 阶段的小型 all_reduce 也能享受 CUDA Graph 回放（见 [u5-l3](u5-l3-cuda-graph.md)）。这是 Overlap Scheduling + CUDA Graph 的通信拼图。

#### 4.5.2 核心流程

初始化分两段（[kernel/pynccl.py:L45-L78](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L45-L78)）：

```
1) 协商 NCCL unique ID（UID）
   - rank 0 调 module.create_nccl_uid() 生成 UID
   - 用 gloo CPU 进程组 broadcast_object_list 把 UID 广播给所有 rank
   - 各 rank 拿到同一 UID 后才能 ncclCommInitRank

2) 构造 NCCLWrapper(rank, world_size, max_size_bytes, uid)
   - ncclCommInitRank 建立通信器
   - ncclMemAlloc(max_bytes) 分配「对称内存」缓冲
   - ncclCommWindowRegister 把缓冲注册成 NCCL_WIN_COLL_SYMMETRIC 窗口
```

「最大缓冲」`max_size_bytes` 的两道闸门（[pynccl.py:L54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L54) 与 [env.py:L70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L70)）：

```
max_size_bytes = min(调用方传入的 max_bytes, ENV.PYNCCL_MAX_BUFFER_SIZE)
                 └─ Engine 算的「最大激活张量字节数」    └─ 默认 1 GiB 上限
```

Engine 侧传入的 `max_bytes` 是「一次前向里最大那块要 all_reduce 的激活张量大小」（[engine.py:L123-L125](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L123-L125)）：

```python
max_bytes = config.max_forward_len * config.model_config.hidden_size * self.dtype.itemsize
```

即 `tokens × hidden_size × dtype字节数`。环境变量 `MINISGL_PYNCCL_MAX_BUFFER_SIZE` 可压低它（默认 1 GiB，见 [env.py:L70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L70)，解析支持 `512M`/`1G` 等后缀，见 [env.py:L40-L47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L40-L47)）。

通信时的「缓冲命中」分支（[csrc/src/pynccl.cu:L105-L133](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L105-L133)）：

```
all_reduce(tensor, "sum"):
  若 tensor 字节数 <= max_bytes：        # 能塞进对称缓冲
      把 tensor 拷进 m_sym_mem（固定地址）
      ncclAllReduce(in=m_sym_mem, out=m_sym_mem)   # 原地归约
      把结果拷回 tensor
  否则：                                  # 装不下
      ncclAllReduce(in=tensor_ptr, out=tensor_ptr)  # 直接在 tensor 上归约
```

塞进固定地址的对称缓冲是关键——这正是「地址固定、内容可变」的 CUDA Graph 录制要求。`all_gather` 不走缓冲、直接 gather 到输出张量（[pynccl.cu:L152-L160](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L152-L160)）。

#### 4.5.3 源码精读

UID 协商与构造（[kernel/pynccl.py:L45-L78](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L45-L78)），关键两段：

```python
max_size_bytes = min(max_size_bytes, ENV.PYNCCL_MAX_BUFFER_SIZE.value)   # 闸门
...
if tp_rank == 0:
    id_list = [module.create_nccl_uid()]
else:
    id_list = [None]
torch.distributed.broadcast_object_list(id_list, src=0, group=tp_cpu_group)  # gloo 广播 UID
nccl_id = id_list[0]
return cls(tp_rank, tp_size, max_size_bytes, nccl_id)
```

注意 UID 协商走的是 **gloo CPU 进程组**（`tp_cpu_group`），而不是 NCCL 自己——因为 NCCL 通信器还没建好，只能借已有的 CPU 组来广播 UID（与 u4-l2 多 rank 用 gloo 同步一脉相承）。

C++ 侧 `NCCLWrapper` 构造（[csrc/src/pynccl.cu:L74-L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L74-L91)）：

```cpp
NCCLWrapper(int rank, int world_size, const size_t max_bytes, NCCLIDList uid)
    : m_rank(rank), m_world_size(world_size), m_max_bytes(max_bytes) {
  ncclUniqueId id = get_uid(uid);
  ncclComm_t comm;
  NCCL_CHECK(::ncclCommInitRank(&comm, m_world_size, id, m_rank));
  m_comm = {comm, template_fn<::ncclCommDestroy>};
  void *buf;
  NCCL_CHECK(::ncclMemAlloc(&buf, max_bytes));            // 对称内存
  m_sym_mem = {buf, template_fn<::ncclMemFree>};
  ncclWindow_t win;
  NCCL_CHECK(::ncclCommWindowRegister(comm, buf, max_bytes, &win,
                                      NCCL_WIN_COLL_SYMMETRIC));  // 注册窗口
  ...
}
```

`all_reduce` 的缓冲命中分支（[csrc/src/pynccl.cu:L105-L123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L105-L123)）：先 `cudaMemcpyAsync` 拷入 `m_sym_mem`、原地 `ncclAllReduce`、再拷回。`NCCLWrapper` 通过 tvm-ffi 的 Object 系统注册为 `minisgl.NCCLWrapper`（[pynccl.cu:L165-L184](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L165-L184)），Python 侧用 `@tvm_ffi.register_object("minisgl.NCCLWrapper")` 接收（[kernel/pynccl.py:L33-L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L33-L42)）。仅支持 `float16` / `bfloat16`（[pynccl.cu:L59-L63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L59-L63)），因为推理激活只可能是这两种。

上层装配链：

- Engine 决定 `max_bytes` 并启用 PyNCCL（[engine.py:L123-L126](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L123-L126)）。
- `enable_pynccl_distributed` 调 `init_pynccl` 并把 `PyNCCLDistributedImpl(comm)` 压入插件栈（[distributed/impl.py:L73-L90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L73-L90)）。
- 之后所有 `DistributedCommunicator().all_reduce/all_gather` 都落到栈顶的 PyNCCL 实现（[impl.py:L44-L60](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L44-L60)）。

`PyNCCLCommunicator` 在 Python 里只是一个 `TYPE_CHECKING` 下的协议（抽象方法签名），运行时其类型是 `Any`，真正的对象是 tvm-ffi 包出来的 `PyNCCLImpl`（见 [pynccl.py:L8-L25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/pynccl.py#L8-L25)）。

#### 4.5.4 代码实践

**实践目标**：跑通多 rank PyNCCL 的正确性测试，理解 UID 协商与对称缓冲。

**操作步骤**：

1. 阅读 [tests/kernel/test_comm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py)。它用 `multiprocessing` spawn 出 `tp_size=4` 个进程，每个进程 `set_tp_info` + 建 gloo 组 + 调 `kernel.init_pynccl(...)`，然后做 `test_correctness` 与 `bench_performance`。
2. 关注 `max_size_bytes` 的两种取法（[test_comm.py:L33-L41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L33-L41)）：`USE_SYMM=0` 时传 `0`（不分配对称缓冲、走直接归约分支），`USE_SYMM=1` 时传 `8192*K*dtype.itemsize`（走对称缓冲分支）。
3. 在多卡机器运行：`python -m tests.kernel.test_comm`（需 ≥4 张 GPU 或修改 `tp_size`）。

**需要观察的现象**：`test_correctness` 中，4 个 rank 各自 `x=ones`，做 4 次 all_reduce(sum) 后 `x` 应为 `4^4=256`（[test_comm.py:L96-L104](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py#L96-L104)）；其中 rank 0 故意 `sleep(1)` 制造「慢节点」，验证其余 rank 会等待（NCCL 是同步集合通信）。

**预期结果**：所有断言通过，日志打印各 rank 的 all_reduce 平均时延与带宽。无多卡环境则做源码阅读，重点理解 UID 广播与对称缓冲两个机制。

> 待本地验证：本测试硬性需要多张同型号 GPU。

#### 4.5.5 小练习与答案

**练习 1**：为什么 NCCL UID 要用 gloo CPU 进程组广播，而不是直接用 NCCL？

**参考答案**：NCCL 通信器尚未建立（正是在建它的过程中），无法用 NCCL 自己传 UID；只能借用已经存在的 gloo CPU 进程组（`tp_cpu_group`）把 rank 0 生成的 UID 广播给所有 rank。各 rank 拿到同一 UID 后才能各自 `ncclCommInitRank`、最终连通。这与 u4-l2 里「多 rank 用 gloo 同步」是同一套基础设施。

**练习 2**：若某次 all_reduce 的张量比 `max_size_bytes` 大，会发生什么？还能被 CUDA Graph 录制吗？

**参考答案**：走 [pynccl.cu:L124-L133](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/src/pynccl.cu#L124-L133) 的 `else` 分支——直接在该张量指针上 `ncclAllReduce`，不经过对称缓冲。这种情况下地址不固定（取决于调用方传入的张量），通常**不适合**被 CUDA Graph 录制；只有走对称缓冲分支（张量装得进固定地址的 `m_sym_mem`）才是 graph-safe 的。这也是 Engine 把 `max_bytes` 设成「最大激活张量大小」的原因——确保正常前向的 all_reduce 都能命中缓冲分支。

**练习 3**：`MINISGL_PYNCCL_MAX_BUFFER_SIZE` 设成 `512M` 会怎样解析？

**参考答案**：`_PARSE_MEM_BYTES("512M")` 去掉末尾 `M` 得 `512`，乘 `1024**2` 得 `512×1048576 = 536870912` 字节（见 [env.py:L40-L47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L40-L47)）。`init_pynccl` 会取 `min(max_bytes, 536870912)` 作为对称缓冲大小。

---

### 4.6 关于 `kernel/__main__.py` 与 kernel 自测入口

> 说明：本节是对学习目标里「`kernel/__main__` 的 JIT 构建/自测入口」的澄清。本项目的 `__main__.py` 实际只做 IDE 配置生成，**不是**自测入口；真正的 kernel 自测在 `tests/kernel/`。

`python -m minisgl.kernel` 执行的是 [kernel/__main__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/__main__.py)，它只定义并运行一个函数 `generate_clangd`（[__main__.py:L4-L47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/__main__.py#L4-L47)），作用是：

1. 调 `nvidia-smi` 查询当前 GPU 的 compute capability（如 `9.0`）。
2. 收集 tvm-ffi 与本项目的 include 路径（`find_include_path()`、`find_dlpack_include_path()`、`DEFAULT_INCLUDE`）。
3. 拼出 `-xcuda --cuda-gpu-arch=sm_90 -std=c++20 ... -isystem...` 编译参数，写入项目根目录的 `.clangd` 文件。

`.clangd` 是给 [clangd](https://clangd.llvm.org/) LSP 服务器读的配置，让 VS Code / Neovim 等编辑器在打开 `csrc/*.cu`、`csrc/*.cpp` 时能正确解析 `#include <tvm/ffi/...>`、`<dlpack/dlpack.h>` 与 CUDA 语法。换言之，它是**开发者体验（DX）工具**，让阅读/修改 kernel 源码时有代码补全与跳转，与运行时编译无关。

真正的 kernel 正确性 / 性能自测分布在 `tests/kernel/` 下，每个文件都「自带 main」、可独立运行：

| 测试文件 | 覆盖对象 | 关键内容 |
|---|---|---|
| [tests/kernel/test_tensor.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_tensor.py) | `test_tensor`（AOT 烟雾测试） | 验证 tvm-ffi 能编译并通过 `TensorMatcher` 形状/stride/dtype 校验。 |
| [tests/kernel/test_index.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_index.py) | `indexing` | 对比 `F.embedding` 基线，含 masked（vocab_range）模式。 |
| [tests/kernel/test_store.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_store.py) | `store_cache` | 对比 `@torch.compile` 的 scatter 写基线。 |
| [tests/kernel/test_comm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/kernel/test_comm.py) | `init_pynccl` / `PyNCCLCommunicator` | spawn 4 进程做 all_reduce/all_gather 正确性 + 带宽基准。 |

它们都用 `compare_memory_kernel_perf`（index/store）或自写计时（comm）来度量「访存带宽 / 通信时延」，这是评估自定义 kernel 是否值得的关键指标——毕竟这些 kernel 干的都是纯搬运/通信活，拼的就是带宽。

## 5. 综合实践

**任务**：整理一份「哪个 kernel 服务于哪个上层功能」的映射表，把本讲的 4 个 kernel 与它们的上层调用点串起来。

**操作步骤**：

1. 在代码库中搜索 `indexing`、`store_cache`、`fast_compare_key`、`init_pynccl`/`PyNCCLCommunicator` 的所有调用点（可用 IDE 全局搜索或 `grep`）。提示：本讲已列出主要调用点，但你应自己复核一遍，看是否有遗漏。
2. 对每个调用点，回答三个问题：它在哪个子系统（embedding / kv-cache / radix-cache / distributed）、在请求生命周期的哪一步（见 [u1-l4](u1-l4-process-architecture.md)）、为什么需要这个 kernel（而不是 PyTorch 自带算子）。
3. 产出一张下表形式的映射表（以下为本讲已确认的结论，可作为模板）：

| Kernel | 上层调用点 | 子系统 | 生命周期阶段 | 为什么需要它 |
|---|---|---|---|---|
| `indexing` | [embedding.py:L36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L36) | 词表并行 embedding | prefill/decode 首层 | 整行 warp 搬运 + masked 置零支持 TP |
| `store_cache` | [mha_pool.py:L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/mha_pool.py#L50) | KV cache 池 | 每层 attention 落 K/V | 按 `out_loc` 分散写池 + 字节数模板特化 |
| `fast_compare_key` | [radix_cache.py:L67](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L67) | radix 前缀缓存 | 调度线程 match_prefix | CPU 向量化 `std::mismatch` 比逐元素循环快 |
| `init_pynccl` | [distributed/impl.py:L83](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L83) | TP 集合通信 | Engine 初始化 + 每层 all_reduce | 对称内存让通信可被 CUDA Graph 录制 |

4. 进阶：补上每个 kernel 的「数据走向」——`indexing`/`store_cache` 是 GPU→GPU（在 engine stream 上），`fast_compare_key` 是 CPU→CPU（在调度线程），PyNCCL 是跨 GPU（NCCL）。据此体会「为什么 `fast_compare_key` 是唯一不上 GPU 的」。

**预期结果**：一张完整的映射表 + 一句话能说清「这 4 个 kernel 分别卡在 LLM 推理数据流的哪个瓶颈点上」。这是把 u4–u9 的调度/执行/通信知识收束到「底层算子」层面的关键一步。

## 6. 本讲小结

- `kernel/` 包用 **tvm-ffi** 把 C++/CUDA 源码编译成 Python 可调用模块；`load_jit` 按模板参数（dtype/尺寸）特化并缓存多份，`load_aot` 构建一次复用，两者都是懒加载。
- `indexing` 用 warp 级 `warp::copy` 整行搬运 embedding，masked 模式（`vocab_range`）把越界 token 置零以支持词表并行，被 `VocabParallelEmbedding` 调用。
- `store_cache` 把新算的 K/V 按 `out_loc` 分散写进 paged KV pool，按每行字节数特化，是每层 attention「先落池再算」的第一步。
- `fast_compare_key` 是唯一的 CPU kernel，用 `std::mismatch` 返回公共前缀长度，服务 radix 树匹配；它走 CPU 是因为数据本就在调度线程、上 GPU 不划算。
- `PyNCCLCommunicator` 在初始化时用 gloo 组广播 NCCL UID、预分配「对称内存窗口」（`max_size_bytes` 取「最大激活张量」与 1 GiB 上限的较小者），让 all_reduce 命中固定地址缓冲、从而可被 CUDA Graph 录制。
- `kernel/__main__.py` 实为 `.clangd` 配置生成器（开发者体验），并非自测入口；kernel 的正确性/性能测试在 `tests/kernel/`，各文件自带 main、可独立运行。

## 7. 下一步学习建议

- 顺着调用链向上：读完本讲后，建议重读 [u7-l2 FlashInfer 后端](u7-l2-flashinfer-backend.md) 里 `forward` 的「先 `store_kv` 再 `wrapper.run`」定序，体会 `store_cache` 在真实注意力计算里的位置。
- 顺着通信链展开：结合 [u9-l1 张量并行](u9-l1-linear-tp-distributed.md) 的插件栈与 [u5-l3 CUDA Graph](u5-l3-cuda-graph.md)，理解「PyNCCL 对称缓冲 → graph-safe all_reduce → decode 阶段可整批录图」这条贯穿第 9–10 单元的主线。
- 动手改一个 kernel：以 [csrc/jit/store.cu](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/csrc/jit/store.cu) 为模板，尝试新增一个导出符号（仿照 `_make_wrapper`），在 Python 侧调用它，跑通 `load_jit` 的完整「源码 → 模板实例化 → 编译 → 调用」闭环。
- 若对 MoE 的 Triton kernel 感兴趣，回到 [u10-l1 Fused MoE](u10-l1-moe-fused.md)，那里讲解了同包下 `kernel/triton/fused_moe.py` 的另一类 kernel 风格。
