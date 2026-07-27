# PyTorch 集成与 ACLGraph 入图

## 1. 本讲目标

在前面的讲义里，我们一直用 `@tilelang.jit` 装饰一个算子，然后像普通函数一样调用它：

```python
func = rms_norm_kernel(M, head_dim, block_M, eps)
out = func(x)          # 首次调用触发 JIT 编译，之后复用缓存
```

这种用法很灵活，但有两个工程上的痛点：

1. **它依赖 JIT**：每次到新机器、新 shape，都要现场编译一次，首次调用有可观延迟；而且调用方必须 import 整个 `tilelang`，算子不能像 `torch.nn.functional` 那样被「当成普通 PyTorch 算子」用。
2. **它是逐个算子下发**：当多个算子串行执行（比如 `RMSNorm → RoPE`）时，每个 `func(x)` 都要经历「Python → ctypes → `lib.call` → 向 stream 下发一次 kernel」的完整 host 路径。如果算子本身算得很快，host 侧的下发与调度开销反而成了瓶颈，这叫 **HostBound**。

本讲就解决这两个问题，对应两个最小模块：

- **`examples/torch_tl_ascend`**：把 tilelang 算子提前编译（AOT）成 `.so`，再用一个极小的 Python C 扩展把它注册成 `torch.ops.tl_ascend.flash_attention`，从而**像原生 PyTorch 算子一样被调用**。
- **`examples/aclgraph/rms_rope_aclgraph.py` + ACLGraph 入图**：用 `torch.npu.NPUGraph` 把一串算子调用**捕获（capture）成一张静态图**，之后一次 `replay()` 整体重放，**绕过逐次的 host 下发**，降低 HostBound 场景的延迟。

学完本讲，你应该能够：

- 说清 tilelang 算子接入 PyTorch / torch-npu 的两种姿势（JIT 直调 vs C 扩展注册），以及 AOT 提前编译与 `.so` 导出的流程。
- 理解 `_inner.cpp` 如何把 `call(...)` 包装成 `at::Tensor` 接口的算子，并用 `TORCH_LIBRARY` 注册到 `torch.ops.tl_ascend` 命名空间。
- 掌握 ACLGraph 的 capture/replay 原理，说清**为什么** tilelang 的 `lib.call` 能被正确捕获（关键在 stream 与 `data_ptr` 的稳定地址）。
- 会用一个 tilelang 算子构造一张 NPU 图，做 capture + replay，并对比与单次调用的耗时。

## 2. 前置知识

阅读本讲前，建议你已掌握：

- **u1-l5（JIT 与运行总流程）**：理解 `@tilelang.jit → compile → lower → bisheng 编译 .so → ctypes/cython 调用` 这条链路，尤其是最后一步——`CythonKernelWrapper.forward` 把 torch 张量的 `data_ptr()` 打包，调 `.so` 里名为 `call` 的符号。本讲的 ACLGraph 部分完全建立在这条运行链上。
- **u7-l1（FlashAttention）**：知道 `T.gemm_v0`、`T.reduce_max/reduce_sum`、`T.tile.*` 这些原语怎么用，本讲会复用 RMSNorm / RoPE 这类「Vector 核逐元素 + 归约」的算子作为入图对象。
- 几个名词：
  - **AOT（Ahead-Of-Time，提前编译）**：与 JIT（即时编译）相对，指在打包/安装阶段就把算子编译成 `.so`，运行时直接加载，省掉现场编译。
  - **torch-npu**：华为提供的 PyTorch 昇腾后端，让 `torch.randn(...).npu()`、`torch.npu.current_stream()` 等接口可用，本讲的张量都落在 `device="npu"` 上。
  - **aclrtStream**：昇腾运行时的命令流（类比 CUDA Stream），kernel 启动指令异步提交到上面。`forward` 运行时取的就是 `torch.npu.current_stream().npu_stream`。
  - **Python C 扩展**：用 C/C++ 写一个能被 Python `import` 的模块（`.so`），常用于把高性能 C++ 函数暴露给 Python。本讲里 `_inner` 就是一个「几乎为空」的 C 扩展，它的真正作用是借 `import` 触发 `TORCH_LIBRARY` 注册。
  - **ACLGraph（aclGraph）**：昇腾的图执行模式，把 eager 模式下「下发即执行」拆成「capture 记录 + replay 重放」两阶段。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 语言 | 职责 |
|------|------|------|
| [examples/torch_tl_ascend/README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/README.md) | 文档 | 集成示例总览：构建、安装、测试、基本用法 |
| [examples/torch_tl_ascend/setup.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/setup.py) | Python | 打包脚本：构建时调 `update_package_files` 抓 `.so`，并用 `CppExtension` 编译 `_inner.cpp` |
| [examples/torch_tl_ascend/compile_tl_op/flash_attention.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/compile_tl_op/flash_attention.py) | Python | AOT 工具：复用 JIT 编译链把 flash_attention 编成 `libop.so`，并拷进包目录 |
| [examples/torch_tl_ascend/compile_tl_op/util/wrap_libgen.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/compile_tl_op/util/wrap_libgen.py) | Python | 给 `LibraryGenerator.load_lib` 打猴子补丁，记住编译出的 `.so` 路径 |
| [examples/torch_tl_ascend/src/_inner.cpp](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/_inner.cpp) | C++ | Python C 模块：定义 `flash_attention_wrapper`、用 `TORCH_LIBRARY` 注册算子、空 `PyInit__inner` 触发注册 |
| [examples/torch_tl_ascend/src/torch_tl_ascend/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/torch_tl_ascend/__init__.py) | Python | 包入口：`import torch_tl_ascend._inner`，借 import 触发算子注册 |
| [examples/torch_tl_ascend/test_torch.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/test_torch.py) | Python | 测试：`torch.ops.tl_ascend.flash_attention(q, k, v)` |
| [examples/torch_tl_ascend/overview.ipynb](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/overview.ipynb) | 文档 | 原理讲解 notebook，逐步演示构建与调用 |
| [examples/aclgraph/rms_rope_aclgraph.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py) | Python | ACLGraph 示例：定义 RMSNorm + RoPE，用 `NPUGraph` capture/replay |
| [examples/aclgraph/README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/README.md) | 文档 | ACLGraph 示例说明：背景、四步流程、验证方法 |
| [tilelang/jit/adapter/cython/cython_wrapper.pyx](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx) | Cython | `forward`：运行时取 NPU stream、打包 `data_ptr`、调 `lib.call`（ACLGraph 能捕获的根因） |
| [tilelang/jit/adapter/cython/adapter.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py) | Python | `CythonKernelAdapter`：`_convert_torch_func` 产出可被 capture 的 `forward` |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 文档 | 官方手册 7.1 节「aclgraph 入图」给出 capture/replay 概念与示例 |

建议对照这张地图阅读后面的源码精读小节。

## 4. 核心概念与源码讲解

### 4.1 从 JIT 到 PyTorch 算子：torch_tl_ascend 集成包

#### 4.1.1 概念说明

回顾 u1-l5：`@tilelang.jit` 装饰后，首次调用 `func(x)` 会现场走一遍「lowering → Ascend C 生成 → bisheng 编译 → 加载」，把结果缓存起来；后续相同 shape 的调用直接复用。这套机制的好处是**灵活、按 shape 编译**，代价是：

- 调用方必须 `import tilelang`，算子「长得不像」PyTorch 自带算子。
- 首次调用的编译延迟，在生产部署、尤其固定 shape 的推理场景里是不必要的。

`examples/torch_tl_ascend` 给出另一种姿势：**在打包/安装阶段（AOT）就把算子编译好**，固化成一个 `.so`，再用一个极小的 **Python C 扩展**把这个 `.so` 里的 `call` 符号包装成 `at::Tensor` 接口的函数，并用 PyTorch 的 `TORCH_LIBRARY` 机制注册到 `torch.ops.tl_ascend` 命名空间。最终用户只要：

```python
import torch_tl_ascend                       # import 即触发注册
out = torch.ops.tl_ascend.flash_attention(q, k, v)
```

就能像调用 `torch.nn.functional` 一样调用它，完全不感知 `tilelang` 的存在。

这里要厘清两件事：

- **AOT 编译出的 `.so` 与 JIT 产物是同一种东西**。AOT 并没有换编译器，它只是把 JIT 链路在「打包时」跑一遍，把产物 `libop.so` 拷进包里。所以 u1-l5 讲的 `call` 符号、host 启动器、bisheng 编译完全适用。
- **「集成包」本身不参与算子计算**，它只是「`.so` + 一个 C 扩展」的打包与注册。算子的所有计算逻辑仍然来自 codegen 生成的 `call`。

> 小贴士：示例固定用 `examples/flash_attention/flash_attn_bhsd` 这一个算子，但 `compile_tl_op` 的工具脚本是通用的——给它换一个被 `@tilelang.jit` 装饰的算子函数，就能照同样流程集成别的算子。

#### 4.1.2 核心流程

整个集成包的「构建 → 安装 → 调用」可以画成下面这条链：

```
python setup.py install
        │
        ├── ① compile_tl_op/flash_attention.py::update_package_files()
        │         └── 复用 JIT 链编译 flash_attention → libop.so
        │         └── shutil.copy(libop.so → src/torch_tl_ascend/libop.so)
        │         └── shutil.copy(flash_attn_bhsd.py → op_source/)   # 源码也打包
        │
        ├── ② CppExtension("torch_tl_ascend._inner", ["src/_inner.cpp"])
        │         └── 链接 libop.so（libraries=["op"]）与 libtorch_npu
        │         └── -Wl,-rpath,$ORIGIN  让 _inner.so 在同目录找 libop.so
        │
        └── 产出 wheel：torch_tl_ascend/_inner.so + libop.so + op_source/*.py

# 运行时
import torch_tl_ascend
        │
        └── __init__.py: import torch_tl_ascend._inner
                  └── 触发 _inner.so 的静态初始化 → TORCH_LIBRARY 注册算子

torch.ops.tl_ascend.flash_attention(q, k, v)
        └── flash_attention_wrapper(Q,K,V)  # _inner.cpp
                  └── 取 c10_npu::getCurrentNPUStream()
                  └── 调 libop.so 里的 call(Q,K,V,out,ws1,ws2,ws3,stream)
```

三个关键点：

1. **`.so` 是 AOT 抓来的**：`setup.py` 在构建最前面就调用 `update_package_files()`，借 JIT 链把算子编出来，再 `shutil.copy` 进包目录。
2. **C 扩展只是胶水**：`_inner.cpp` 用 `TORCH_LIBRARY` 注册算子，用 `PyInit__inner` 提供一个能被 `import` 的空模块——`import` 这个动作触发了注册。
3. **两个 `.so` 互链**：`_inner.so`（C 扩展，g++ 编）链接 `libop.so`（算子，bisheng 编）。靠 `-rpath,$ORIGIN` 让前者在自身所在目录找到后者。

#### 4.1.3 源码精读

**(a) AOT 抓 `.so`：`update_package_files`**

`setup.py` 一开头就调用了它，这是 AOT 的核心：

[examples/torch_tl_ascend/compile_tl_op/flash_attention.py:49-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/compile_tl_op/flash_attention.py#L49-L66) —— `update_package_files` 做两件事：把算子源码 `flash_attn_bhsd.py` 拷进 `op_source/`；用固定 shape `B,S,H,D = 4,4096,16,128` 调 `get_kernel` 编译，再把产出的 `libop.so` 拷进包目录。

注意它复用的就是 JIT 链：

[examples/torch_tl_ascend/compile_tl_op/flash_attention.py:32-47](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/compile_tl_op/flash_attention.py#L32-L47) —— `get_kernel` 直接调被 `@tilelang.jit` 装饰的 `op_func`，得到 `JITKernel`；`so_path_of` 从 `kernel.adapter.lib_generator.libpath` 取出编译好的 `.so` 路径。

这里有个小技巧：`lib_generator.libpath` 本来不是 `LibraryGenerator` 的标准属性。它靠 `wrap_libgen.py` 的猴子补丁注入：

[examples/torch_tl_ascend/compile_tl_op/util/wrap_libgen.py:5-14](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/compile_tl_op/util/wrap_libgen.py#L5-L14) —— 包装 `LibraryGenerator.load_lib`，加载完后把 `lib_path` 记到 `self.libpath`，这样打包脚本才知道 `.so` 落在了哪。

**(b) 打包并链接：`setup.py`**

[examples/torch_tl_ascend/setup.py:18-41](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/setup.py#L18-L41) —— 关键几行：

- `package_data={"torch_tl_ascend": ["*.so"]}`：把 `libop.so` 当包数据打进 wheel。
- `CppExtension("torch_tl_ascend._inner", [..., "_inner.cpp"], ...)`：用 PyTorch 提供的 `CppExtension`（基于 `torch.utils.cpp_extension`）编译 C 扩展。
- `libraries=["torch_npu"] + ["op"]`：`libop.so` 去前缀 `lib` 后就是 `op`，所以链接 `-lop`；同时链接 `torch_npu`（用到 `c10_npu::getCurrentNPUStream`）。
- `extra_link_args=["-Wl,-rpath,$ORIGIN"]`：运行时让 `_inner.so` 在**自身所在目录**（`$ORIGIN`）找 `libop.so`，这正是二者被打包到同一目录的前提。

> 名词解释：`$ORIGIN` 是动态链接器的一个特殊 token，指「`.so` 文件自身所在目录」。设了 `-rpath,$ORIGIN` 后，`_inner.so` 不依赖 `LD_LIBRARY_PATH` 就能找到同目录的 `libop.so`，部署更干净。

**(c) C 扩展胶水：`_inner.cpp`**

这是集成包的灵魂，分三段看。

第一段——声明算子的 host 入口 `call`，与 u6-l4 讲的 codegen 产物完全对齐：

[examples/torch_tl_ascend/src/_inner.cpp:8-15](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/_inner.cpp#L8-L15) —— `extern "C" void call(...)` 的签名固定为「3 个输入 + 1 个输出 + 3 个 workspace + 1 个 stream」，这正是 flash_attention codegen 生成的 host 启动器签名。

第二段——把 `call` 包装成接收/返回 `at::Tensor` 的 PyTorch 算子：

[examples/torch_tl_ascend/src/_inner.cpp:17-56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/_inner.cpp#L17-L56) —— `flash_attention_wrapper` 做四件事：① 校验 Q/K/V 都是 4-D；② 用 Q 的 shape 推出 `batch/heads/seq_len/dim` 与 `block_num`；③ **在 C++ 里直接 `at::empty_like` / `at::empty` 分配输出与三块 workspace**（对应 u5-l4 的跨核 workspace）；④ 取 `c10_npu::getCurrentNPUStream().stream(false)`，把每个张量的 `data_ptr()` 转成 `uint8_t*` 后调 `call`。

第三段——注册到 PyTorch 命名空间，并提供「能被 import 的空模块」：

[examples/torch_tl_ascend/src/_inner.cpp:58-68](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/_inner.cpp#L58-L68) —— `TORCH_LIBRARY(tl_ascend, m)` 声明算子签名 `flash_attention(Tensor Q, Tensor K, Tensor V) -> Tensor`；`TORCH_LIBRARY_IMPL(..., PrivateUse1, m)` 把实现挂到 `PrivateUse1` 分发键——这正是 torch-npu 这类自定义后端注册算子实现用的键。`Meta` 那一份是无真实设备时的占位实现。

> 名词解释：`PrivateUse1` 是 PyTorch 预留给第三方后端（如 XLA、NPU）的调度键。torch-npu 把 NPU 张量归到 `PrivateUse1`，所以算子实现注册到这里，`torch.ops.tl_ascend.flash_attention` 在 NPU 张量上调用时就能 dispatch 到 `flash_attention_wrapper`。

[examples/torch_tl_ascend/src/_inner.cpp:71-88](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/_inner.cpp#L71-L88) —— `PyInit__inner` 定义了一个**内容为空的 Python 模块**。它的唯一意义是：Python 一旦 `import torch_tl_ascend._inner`，动态加载器就会执行 `_inner.so` 的全局构造，从而跑到上面 `TORCH_LIBRARY` 的静态初始化，完成注册。注释里写得很直白：「The import from Python will load the .so ... so that the TORCH_LIBRARY static initializers are run.」

**(d) 包入口：import 触发注册**

[examples/torch_tl_ascend/src/torch_tl_ascend/__init__.py:1](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/src/torch_tl_ascend/__init__.py#L1) —— 整个包入口只有一行 `import torch_tl_ascend._inner`，目的就是触发那段静态注册。这就是为什么 `test_torch.py` 里 `import torch_tl_ascend` 之后，`torch.ops.tl_ascend.flash_attention` 就直接可用了。

**(e) 调用侧：像原生算子一样用**

[examples/torch_tl_ascend/test_torch.py:29](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/test_torch.py#L29) —— `output = torch.ops.tl_ascend.flash_attention(q, k, v)`，调用方完全不需要 import tilelang，与 u1-l5 里 `func(q, k, v)` 的 JIT 写法形成对照。对照参考实现 `ref_flash_attention` 做 `assert_close`，通过即打印 `Test Passed!`。

#### 4.1.4 代码实践

**实践目标**：理解「`.so` 是怎么进包的」，亲手跑一遍 AOT 抓取流程，并验证 C 扩展注册生效。

**操作步骤**（需具备真实昇腾环境，否则标注处为「待本地验证」）：

1. 阅读并理解 [examples/torch_tl_ascend/overview.ipynb](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/overview.ipynb) 的「原理解析」一节，对照 4.1.2 的流程图。
2. 在 `examples/torch_tl_ascend/` 下执行 `python setup.py install`，观察构建日志：先看到 `Grabbing .so of <function flash_attention_fwd ...>`（即 AOT 编译触发），再看到 C 扩展编译。
3. 构建完成后，检查安装产物里是否同时存在 `torch_tl_ascend/_inner.*.so` 与 `libop.so`（`pip show -f torch-tl-ascend`）。
4. 运行 `python test_torch.py`，期望输出 `init successful!` 与 `Test Passed!`。

**需要观察的现象**：

- 步骤 2 里「Grabbing .so」只在**构建期**出现一次；运行 `test_torch.py` 时不再有任何编译动作（对比 JIT 首次调用会打印编译日志）。
- 若把 `src/_inner.cpp` 里的 `PyInit__inner` 改名（破坏 import），再重装，`torch.ops.tl_ascend` 会在调用时报「算子未定义」——这验证了 import 触发注册。

**预期结果**：`Test Passed!`。若没有真实 NPU，本步骤为「待本地验证」。

> 扩展：`demo_libtorch/` 子目录给出了同一套思路的 **C++ 版**（用 libtorch + libtorch_npu）。它的 [flash_attention.cpp](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/torch_tl_ascend/demo_libtorch/flash_attention.cpp) 同样定义 `flash_attention_fwd`、取 `c10_npu::getCurrentNPUStream()`（见其第 103、136 行），区别只是宿主从 Python 换成 C++ `main()`。原理与本节一致，可作为拓展阅读。

#### 4.1.5 小练习与答案

**练习 1**：`_inner.cpp` 里为什么必须用 `PrivateUse1` 这个分发键，而不能用 `CUDA` 或 `CPU`？

**参考答案**：因为算子的输入张量是 NPU 张量，torch-npu 把 NPU 后端注册在 `PrivateUse1` 这个预留键上。算子实现挂到 `PrivateUse1`，才能在 NPU 张量上被正确 dispatch；挂到 `CUDA`/`CPU` 只会在对应后端张量上生效，NPU 张量找不到实现。

**练习 2**：如果把 `setup.py` 里的 `extra_link_args=["-Wl,-rpath,$ORIGIN"]` 删掉，运行时 `import torch_tl_ascend` 会发生什么？

**参考答案**：`_inner.so` 链接了 `libop.so`，但没了 `$ORIGIN` 这个 rpath，动态加载器就不知道去包目录找 `libop.so`，`import` 时会报类似 `libop.so: cannot open shared object file` 的错误。补救办法是设置 `LD_LIBRARY_PATH` 指向包目录，但不如 rpath 干净。

---

### 4.2 ACLGraph 入图：capture/replay 加速

#### 4.2.1 概念说明

即便算子已经能用 `torch.ops` 或 `func(x)` 调用，当一串算子要顺序执行时，每次调用都要走一遍 host 路径：

```
Python → (可能的 ctypes) → lib.call → aclrt 向 stream 下发一条 kernel 启动指令
```

对单个大算子（比如一次完整 GEMM、一次 FlashAttention），device 上的计算时间远大于这一次 host 下发，host 开销可忽略。但当算子**又小又多**（比如推理里的 `RMSNorm → RoPE`、多个 elementwise），每个算子的 device 执行很快，CPU 端却在「逐个下发、逐个等返回」上耗时间——这时的瓶颈在 host，称为 **HostBound**。

ACLGraph（`torch.npu.NPUGraph`）就是为缓解 HostBound 设计的。官方手册 [docs/TileLang-Ascend Programming Guide.md:2461-2466](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2461-L2466) 对它的定位是：

> aclGraph 通过将 eager 模式任务下发即执行区分为 **capture + replay** 两阶段，通过 1 次 capture + 多次 replay 减小交互开销，对 HostBound 场景做运行时加速，**不承载图编译和图优化功能**。

注意最后一句：ACLGraph **不是**编译器，它不会做算子融合或内存优化，它只是把「一连串设备下发指令」录制成一张静态图，之后一条命令整体重放。它的收益纯粹来自**省掉逐次的 host 交互**。

用公式直观看 eager vs replay 的差别。设一串共 \(N\) 个算子，第 \(i\) 个的 host 下发开销为 \(h_i\)、device 执行时间为 \(d_i\)，eager 模式下二者串行（host 必须逐个下发）：

\[
T_{\text{eager}} \approx \sum_{i=1}^{N}(h_i + d_i)
\]

当 \(h_i\) 与 \(d_i\) 同量级（小算子），\(T_{\text{eager}}\) 里 host 占比很高。capture 把这 \(N\) 个下发动作**记录**成一张图，replay 时整体提交，host 侧开销近似摊到一次提交：

\[
T_{\text{replay}} \approx H_{\text{replay}} + \sum_{i=1}^{N} d_i,\qquad H_{\text{replay}} \ll \sum_{i=1}^{N} h_i
\]

所以对纯 HostBound 场景，加速比近似为：

\[
\text{加速比} \approx \frac{\sum(h_i+d_i)}{H_{\text{replay}}+\sum d_i}\;\xrightarrow{\;h_i\gg d_i\;}\;\frac{\sum h_i}{H_{\text{replay}}}
\]

这也解释了**什么时候该用 ACLGraph**：算子小而多、host 开销占比高时收益大；单个巨型算子收益小。

#### 4.2.2 核心流程

ACLGraph 的用法就三步（见 [docs/TileLang-Ascend Programming Guide.md:2468-2480](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2468-L2480)）：

```
1. 创建 NPUGraph 对象          g = torch.npu.NPUGraph()
2. 捕获算子调用序列             with torch.npu.graph(g):  q = rms(q); q = rope(q, sin, cos)
3. 重放执行（可多次调用）        g.replay()
```

但对我们更重要的是搞懂一个**为什么**：tilelang 的算子调用链里掺了 Python、ctypes、Cython，ACLGraph 凭什么能把它们正确「录制」？答案是它录制的不是 Python 层，而是**设备层**——具体来说，录制的是提交到 stream 上的那些 kernel 启动指令。tilelang 的 `forward` 恰好满足两个前提：

1. **它向「当前 stream」下发指令**：`forward` 默认从 `torch.npu.current_stream().npu_stream` 取 stream（capture 期间这就是 capture stream），所有 `lib.call` 都提交到这条 stream 上，于是这些下发能被 NPUGraph 录制。
2. **它用的是张量的稳定地址**：`forward` 把每个 torch 张量的 `data_ptr()` 作为指针传给 `call`，而 capture 阶段分配的 NPU 张量地址在后续 replay 中保持不变，重放时同一组地址上的同一组指令就能复现整个计算。

`_inner.cpp` 里的 `flash_attention_wrapper` 也满足这两点（取 `c10_npu::getCurrentNPUStream()`、传 `data_ptr()`），所以无论走 JIT 的 `func(x)` 还是走 `torch.ops.tl_ascend.*`，都能被 ACLGraph 捕获——本讲 4.1 与 4.2 在「stream + 稳定地址」这一点上是统一的。

完整的 capture/replay 时序如下：

```
capture 阶段（with torch.npu.graph(g):）
   ┌─ tilelang_rms_norm(q)
   │     └─ forward → 取 current_stream(=capture stream)
   │     └─ lib.call(...) → 向 stream 录制 [rms kernel 启动]
   └─ tilelang_apply_rope(q, sin, cos)
         └─ forward → lib.call(...) → 向 stream 录制 [rope kernel 启动]
   g 内部：把上面两条录制指令存成静态图（host 不真正等待执行）

replay 阶段（g.replay()）
   └─ 一次性把整张图提交到 stream → [rms][rope] 连续执行，host 只下发一次
```

#### 4.2.3 源码精读

**(a) 运行链的根因：`forward` 取 stream、传 `data_ptr`**

先回到 u1-l5 讲过的运行时入口，这次重点看它**为什么能被 capture**：

[tilelang/jit/adapter/cython/cython_wrapper.pyx:86-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L86-L90) —— 当 `stream == -1`（默认值，即用户没显式传 stream）时，从 `torch.npu.current_stream().npu_stream` 取当前流。这正是 ACLGraph capture 的钩子：capture 期间 `current_stream()` 返回的是 capture 专用的 stream，于是录制的指令落在图里。

[tilelang/jit/adapter/cython/cython_wrapper.pyx:150](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L150) 与 [tilelang/jit/adapter/cython/cython_wrapper.pyx:194-197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L194-L197) —— 每个张量用 `tensor.data_ptr()` 转 `c_void_p`，再把 stream 追加为最后一个参数，调 `self.lib.call(*call_args)`。注意整段 `forward` 里**没有任何同步等待**（没有 `synchronize`），它只是「打包指针 + 下发」，所以天然适合被录制。

[tilelang/jit/adapter/cython/adapter.py:451-457](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L451-L457) —— `_convert_torch_func` 产出的 `lambda_forward(*args, stream=-1)` 就是 `JITKernel.__call__` 最终调用的可调用对象（见 [tilelang/jit/kernel.py:184-201](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184-L201) 的 `self.torch_function(*modify_args)`）。也就是说，无论你写 `func(x)` 还是把它放进 `with torch.npu.graph(g):`，走的是同一条 `forward`，区别只在 capture 期间 `current_stream()` 是 capture 流。

**(b) 示例：RMSNorm + RoPE 的 pass_configs**

[examples/aclgraph/rms_rope_aclgraph.py:8-13](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L8-L13) —— 入图示例统一打开四个 pass 开关：`AUTO_SYNC`（自动核内同步，见 u4-l3）、`MEMORY_PLANNING`（缓冲复用，见 u6-l5）、`AUTO_CV_SYNC` + `AUTO_CV_COMBINE`（Cube/Vector 自动分离与核间同步，见 u5-l1）。这组配置让 Developer 写法也能正确产生跨核与核内同步——这对入图很重要，因为图重放时不会再回到 Python 重新插同步，所有同步必须在编译期就插好。

> 复习：这四个 `PassConfigKey` 的含义在 [examples/aclgraph/README.md:41-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/README.md#L41-L46) 有对照表。`AUTO_CV_SYNC` 与 `AUTO_CV_COMBINE` 必须同时开（u5-l1 讲过：核间同步依赖 CV 拆分的产出）。

**(c) 被入图的算子：RMSNorm kernel**

[examples/aclgraph/rms_rope_aclgraph.py:18-61](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L18-L61) —— RMSNorm kernel 用 `@tilelang.jit(out_idx=[-1])` 声明（`out` 由框架自动分配返回），主体是典型的 Vector 流程：`T.copy` 把 GM 搬到 UB、转 fp32、`T.tile.mul` 算平方、`T.reduce_sum` 沿最后一维收缩、再除以维数加 eps 开根号、最后逐行除回去写回 GM。这些原语（u3-l2 搬运、u3-l4 reduce、u3-l5 parallel/tile）在编译期都会被 pass 处理成确定指令，运行时只是「下发执行」，所以能被录制成图。

[examples/aclgraph/rms_rope_aclgraph.py:64-74](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L64-L74) —— Python 包装函数 `tilelang_rms_norm`：把 3D 的 `[batch, head, hidden]` reshape 成 2D `[total_batch, hidden]`，按固定 `block_M=32` 编译 kernel 并调用。它会被 capture 录制。

> 注意 [examples/aclgraph/rms_rope_aclgraph.py:78](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L78) 的 RoPE kernel **没有** `out_idx`——因为它是**原地（in-place）**算子，直接改写输入 `x`，不分配输出张量。这点 README 也专门提醒（见 [examples/aclgraph/README.md:99](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/README.md#L99)）。in-place 在入图时反而更省事：少一次输出张量分配，replay 时地址更稳定。

**(d) capture / replay 三步**

[examples/aclgraph/rms_rope_aclgraph.py:270-279](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L270-L279) —— 这是 ACLGraph 的核心三步：

- `g = torch.npu.NPUGraph()` 创建图对象；
- `with torch.npu.graph(g):` 里依次调 `tilelang_rms_norm` 与 `tilelang_apply_rope`——这两个调用此时**只录制不执行**，两条 `lib.call` 被存进图；
- `g.replay()` 整体重放，随后 `assert_close(q, q_ref)` 校验，通过打印 `Kernel Output Match!`。

注意 [examples/aclgraph/rms_rope_aclgraph.py:247](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/rms_rope_aclgraph.py#L247) 的 `tilelang.disable_cache()`：示例关掉了磁盘缓存，保证每次跑都用最新编译产物，方便调试（生产里通常保留缓存）。

**如何确认 ACLGraph 真的生效**：[examples/aclgraph/README.md:131-139](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/aclgraph/README.md#L131-L139) 给出方法——设 `ASCEND_GLOBAL_LOG_LEVEL=1` 等环境变量后，在 Host 编译日志里搜 `capture model` 关键字，`1` 表示入图已启用。

#### 4.2.4 代码实践

**实践目标**：把一个 tilelang 算子（RMSNorm）封装成可被 NPUGraph 捕获的调用，做 capture + replay，并定量对比「单次 eager 调用 N 次」与「capture 一次 + replay N 次」的耗时，直观感受 ACLGraph 对 HostBound 的加速。

**操作步骤**（需真实昇腾环境，否则为「待本地验证」）：

1. 先跑通现成示例：`cd examples/aclgraph && python rms_rope_aclgraph.py`，期望末尾打印 `Kernel Output Match!`。
2. 在该文件基础上，新增一段计时对比（**示例代码**，非项目原有代码）：

   ```python
   import time
   # 假设 q / sin / cos / variance_epsilon 已就绪，与 main 中一致
   M, block_M, hidden_size = ...        # 复用示例里的维度
   # warmup + 编译
   for _ in range(3):
       _ = tilelang_rms_norm(q.clone(), variance_epsilon)
   torch.npu.synchronize()

   # (A) eager：连续调用 N 次
   N = 50
   t0 = time.perf_counter()
   for _ in range(N):
       tmp = tilelang_rms_norm(q.clone(), variance_epsilon)
   torch.npu.synchronize()
   t_eager = (time.perf_counter() - t0) / N

   # (B) capture + replay
   g = torch.npu.NPUGraph()
   qg = q.clone()
   with torch.npu.graph(g):
       qg = tilelang_rms_norm(qg, variance_epsilon)
   # warmup replay
   for _ in range(3):
       g.replay()
   torch.npu.synchronize()
   t0 = time.perf_counter()
   for _ in range(N):
       g.replay()
   torch.npu.synchronize()
   t_replay = (time.perf_counter() - t0) / N

   print(f"eager/launch = {t_eager*1e6:.1f} us, replay = {t_replay*1e6:.1f} us")
   ```

3. 改大/改小 RMSNorm 的 `block_M` 与 `hidden_size`，重复计时，观察「单算子足够大时二者差距缩小」。

**需要观察的现象**：

- 单个 RMSNorm 这种偏小算子，`t_eager` 里 host 下发占比明显，`t_replay` 应显著更小。
- 当算子变大（`hidden_size`、`block_M` 增大），device 计算时间上升，eager 与 replay 的相对差距收敛——印证 4.2.1 的 HostBound 直觉。

**预期结果**：`replay` 的单次耗时低于 `eager` 的单次下发耗时；算子越大差距越小。若没有真实 NPU，本步骤为「待本地验证」。

> 踩坑提醒：capture 阶段涉及的输入张量地址要在 replay 时保持有效。示例里 in-place 的 RoPE 与 reshape 后的 `q` 都满足了这一点；若你在 capture 里分配了临时张量，注意它们的生命周期要覆盖后续所有 `replay()`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `forward` 里的 `torch.npu.current_stream().npu_stream` 改成写死的 `stream=0`（默认流），ACLGraph 还能正确捕获吗？为什么？

**参考答案**：不能正确捕获。capture 期间 NPUGraph 用的是一条专用的 capture stream，算子必须向**这条** stream 下发指令才能被录制。写死成默认流（0）会把指令下发到默认流而非 capture 流，图里就录不到这些 kernel，replay 时等于空跑或行为错乱。这也说明 `forward`「取 current_stream」是个对入图友好的设计。

**练习 2**：ACLGraph 能不能把 `RMSNorm → RoPE` 这两个算子**融合**成一个新算子？为什么？

**参考答案**：不能。如手册所述，ACLGraph「不承载图编译和图优化功能」，它只录制和重放设备下发序列，不做算子融合。融合要在编译期（TileLang 的 pass 或下游编译器）完成。ACLGraph 的收益只来自省 host 下发开销，不来自减少 device 计算。

**练习 3**：为什么入图示例要同时打开 `AUTO_CV_SYNC` 和 `AUTO_CV_COMBINE`，而不是手写同步？

**参考答案**：图在 replay 时不再回到 Python，也不会重新执行 pass，所以所有跨核/核内同步必须在编译期就插好。打开这两个开关让编译器自动插入 Cube/Vector 核间同步（u5-l1）；如果手写同步也能工作，但 Developer 写法下让 pass 自动插更省心、也更不容易在入图后漏插同步导致数据竞争。

## 5. 综合实践

把 4.1 与 4.2 串起来，完成一个**「集成 + 入图」**的小任务：

**任务**：把 RMSNorm（或 RoPE）算子，先用 4.1 的「C 扩展」思路注册成一个 `torch.ops.tl_ascend.rms_norm` 算子，再用 4.2 的 ACLGraph 把它与另一个算子（例如一次 elementwise 的 `relu` 或一次 GEMM）串成一张 NPU 图，对比三种调用的端到端耗时：

1. **JIT 直调**：`func(x)` 连续 N 次；
2. **torch.ops 调用**：`torch.ops.tl_ascend.rms_norm(x)` 连续 N 次；
3. **ACLGraph replay**：capture 一次后 `g.replay()` 连续 N 次。

**建议步骤**：

1. 参考 `compile_tl_op/flash_attention.py`，把 RMSNorm 的 `@tilelang.jit` 函数用同样流程编出 `libop.so`（注意 RMSNorm 的 `call` 签名与 flash_attention 不同，`_inner.cpp` 里的 wrapper 与 workspace 数量要相应调整）。
2. 在 `_inner.cpp` 里仿照 `flash_attention_wrapper` 写一个 `rms_norm_wrapper`，用 `TORCH_LIBRARY` 追加注册 `m.def("rms_norm(Tensor x) -> Tensor")`。
3. 三条调用路径分别计时（参照 4.2.4 的计时模板），记录「每次平均耗时」与「加速比」。
4. 观察并解释：JIT 直调与 torch.ops 调用哪个更快？为什么？replay 相对二者加速多少？算子变大后趋势如何？

**验收**：三者在数值上都应与 PyTorch 参考实现 `assert_close` 通过；计时上 replay 最快，且算子越小相对加速越明显。

> 提示：`rms_norm_wrapper` 里不需要 flash_attention 那么多 workspace——RMSNorm 是单 Vector 核算子，workspace 数量取决于你的 kernel 是否用了 `workspace_idx`/`auto_gm_idx`（u5-l4）。可先用 `func.get_kernel_source()`（见 [tilelang/jit/kernel.py:378-388](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L378-L388)）查看生成的 `call` 签名，再据此写 wrapper。

## 6. 本讲小结

- **集成包 = AOT 抓 `.so` + C 扩展注册**：`compile_tl_op` 复用 JIT 链在打包时把算子编成 `libop.so`，`_inner.cpp` 用 `TORCH_LIBRARY` 把 `call` 包装成 `at::Tensor` 算子、注册到 `torch.ops.tl_ascend`，靠 `import torch_tl_ascend._inner` 触发静态注册。调用方完全无需感知 tilelang。
- **AOT 与 JIT 编出的 `.so` 是同一种东西**：AOT 没换编译器，只是把 JIT 链提前到打包阶段跑一遍；`call` 符号、host 启动器、bisheng 编译都一样。
- **`$ORIGIN` rpath 与 `PrivateUse1` 是两个关键细节**：前者让 `_inner.so` 在包目录找到 `libop.so`，后者是 torch-npu 注册 NPU 算子实现的分发键。
- **ACLGraph 解决 HostBound**：它把「逐次 host 下发」录制成静态图，一次 `replay()` 整体重放，省掉 host 交互；但它**不做算子融合/编译优化**，收益纯粹来自省 host 开销，算子越小越多收益越大。
- **tilelang 能被 ACLGraph 捕获的根因**：`forward` 向「当前 stream」下发指令、用张量的 `data_ptr()` 稳定地址调 `lib.call`，且全程无同步等待——capture 期间 `current_stream()` 就是 capture 流，于是录制自然成立。这条结论对 JIT 的 `func(x)` 与 `torch.ops.tl_ascend.*` 一视同仁。
- **入图要确保同步在编译期插好**：示例统一打开 `AUTO_SYNC`/`MEMORY_PLANNING`/`AUTO_CV_SYNC`/`AUTO_CV_COMBINE`，因为图重放时不再回 Python、不再跑 pass。

## 7. 下一步学习建议

- **性能与调参**：本讲的 ACLGraph 关注「省 host 开销」，而单算子的 device 性能取决于 u7-l2（高性能 GEMM 优化）讲的 layout/swizzle/pipeline/kL0Size 调参。建议把「ACLGraph 省交互 + 单算子调到极致」结合，做端到端优化。
- **A5 仿真验证**：如果你的入图算子要在 camodel 上仿真验证，参看 u7-l5（A5 仿真运行），注意仿真仅支持 PTO 后端、不支持 `torch.npu`，所以 NPUGraph 这类依赖 `torch.npu` 的流程无法直接在仿真里跑，需要先拆成「算子级」验证。
- **调试生成代码**：写 `_inner.cpp` 的 wrapper 前，用 `func.get_kernel_source()`（u7-l4 调试与性能分析）确认 `call` 的确切签名与 workspace 数量，避免参数错配。
- **自动调参与集成结合**：在把算子固化进集成包之前，可以先用 u7-l6（Autotuner）扫一遍 `block_M`/`block_N` 等参数空间，挑出最优配置再 AOT 编译，避免把次优实现固化进 `.so`。
- **扩展阅读源码**：`examples/torch_tl_ascend/demo_libtorch/flash_attention.cpp`（C++ 集成版）、`examples/aclgraph/README.md`（ACLGraph 四步法与验证方法）是本讲两个例子的最佳补充读物。
