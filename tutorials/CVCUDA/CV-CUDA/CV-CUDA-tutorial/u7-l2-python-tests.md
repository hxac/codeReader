# Python 测试：pytest 与 internal 白盒子模块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `tests/cvcuda/python` 目录的组织规律：每个算子一个 `test_op*.py`，加上 `cvcuda_util.py` / `cvcuda_tools.py` / `cvcuda_types.py` 三个基础设施模块。
2. 运行单个 pytest 文件（或用 `run_tests.sh cvcuda,python` 跑整套），并解释 `cvcuda_test_python` 包装脚本在 pytest 之前做的三件事。
3. 读懂 `test_opflip.py` 的断言策略：手写测试只断言**元数据契约**（shape/layout/dtype/批属性），数值正确性由 C++ 系统测试（u7-l1）负责。
4. 理解 `cvcuda_tools.make_op_tests` 这个"契约矩阵工厂"：声明一份支持矩阵，它自动生成正例与负例测试，并通过 `globals().update()` 注册进 pytest。
5. 说明 `cvcuda.internal` 与 `cvcuda._test` 两个白盒子子模块的设计动机与使用方式，并能仿照 `test_opremap.py` 为变长批 flip 写出**带数值断言**的测试。

## 2. 前置知识

- **黑盒测试 vs 白盒测试**：黑盒测试只通过公开接口观察输入与输出，不关心内部状态；白盒测试则主动窥探实现内部（比如查询对象缓存占了多少字节）。CV-CUDA 的哲学是：公开 API 面向用户保持稳定，内部机制随时可改——所以白盒观测点被集中放进 `cvcuda.internal` 和 `cvcuda._test` 两个明确标注"仅测试用"的子模块，与公开 API 划清界限。
- **pytest 参数化**：`@pytest.mark.parametrize("参数名", [值列表])` 让同一个测试函数按参数展开成多个独立用例，每个用例有自己的 ID，失败时能精确定位是哪组参数挂了。
- **函数工厂（测试生成器）**：Python 允许在模块导入期用代码动态创建函数并塞进 `globals()`。pytest 收集测试时扫描的就是模块的全局命名空间，所以"生成函数 → 注入 globals()"等价于"写了很多个测试函数"。这是 `make_op_tests` 的核心手法。
- **集合差集生成负例**：若全库共有 11 种标量 dtype（`SCALAR_TYPES_SET`），某算子只支持其中 4 种，那么负例集合自动为 \( D_{all} \setminus D_{sup} \)（全集减支持集），共 7 个——不需要手写任何一个负例。
- 建议先完成 u7-l1（C++ 系统测试的"CPU 黄金参考"范式）与本手册 u3-l1（flip 算子的四连函数）、u4-l2（Python 对象缓存），本讲会频繁对照这三讲的概念。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tests/cvcuda/python/test_opflip.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py) | Flip 算子的 Python 测试：手写元数据断言 + 调用工厂生成契约矩阵测试 |
| [tests/cvcuda/python/cvcuda_tools.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py) | 测试生成框架：`make_op_tests` 按支持矩阵批量产出正/负 pytest 用例 |
| [tests/cvcuda/python/cvcuda_util.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py) | 数据构造工具箱：随机张量/图像/变长批的创建与克隆 |
| [tests/cvcuda/python/cvcuda_types.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_types.py) | 类型系统对照表：Format/Type/numpy/torch 互转、布局到形状的解析、测试用的全集常量 |
| [tests/cvcuda/python/test_cache.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py) | 对象缓存的白盒测试：大量使用 `cvcuda.internal.nbytes_in_cache` |
| [tests/cvcuda/python/cvcuda_test_python.in](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in) | 测试运行包装脚本模板（构建期由 CMake 填充路径生成） |
| [python/mod_cvcuda/Main.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp) | pybind11 模块入口：创建 `internal` 与 `_test` 两个白盒子模块 |
| [python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) | 缓存公开 API 与 `internal.nbytes_in_cache` 的导出处 |
| [tests/cvcuda/python/test_opremap.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opremap.py) | 本讲的"数值断言范本"：变长批结果逐像素比对的官方写法 |

## 4. 核心概念与源码讲解

### 4.1 Python 测试套件的组织与运行方式

#### 4.1.1 概念说明

`tests/cvcuda/python` 是一个**扁平的 pytest 目录**，共 94 个文件，按文件名就能判断用途：

- **`test_op*.py`（约 60 个）**：每个算子一个，命名是 `test_op` + 算子小写蛇形名（`test_opflip.py`、`test_opresize.py`、`test_ophqresize.py`……），与 `python/mod_cvcuda/operators/Op*.cpp` 一一呼应。
- **三个基础设施模块**（不带 `test_` 前缀，pytest 不会收集它们）：`cvcuda_util.py` 造数据、`cvcuda_tools.py` 造测试、`cvcuda_types.py` 存类型对照表。
- **类型层测试**：`test_datatype.py`、`test_imgformat.py`、`test_tensor.py`、`test_image.py`、`test_imgbatchvarshape.py`、`test_stream.py` 等，对应 nvcv 类型层的 Python 绑定。
- **专项机制测试**：`test_cache.py`（对象缓存）、`test_multi_stream.py` / `test_multi_threading.py` / `test_multi_gpu.py`（并发）、`test_resourceguard.py` / `test_resource_submitsync.py`（资源守卫）、`test_nvtx_markers.py`（NVTX 埋点）、`test_import_order.py`、`test_version.py` 等。

与 C++ 系统测试（u7-l1）的分工是理解本讲的钥匙：**C++ 侧用 CPU 黄金参考做逐字节的数值校验；Python 侧主要验证 Python API 的调用契约**——参数能传对、输出元数据正确、不支持的组合会抛异常、以及 Python 特有机制（缓存、流栈、线程）的行为。

#### 4.1.2 核心流程

一次完整的 Python 测试运行经过四步：

1. 构建期：CMake 把 `cvcuda_test_python.in` 填入路径，生成 `build-rel/bin/cvcuda_test_python` 包装脚本。
2. 依赖检查：脚本对每个候选 Python 版本执行 `import pytest, cupy, numpy`，缺依赖的版本被跳过（除非设 `FORCE_PYTHON_TESTS`）。
3. 符号检查：`readelf` 检查 `_cvcuda` 扩展模块只导出 `PyInit__cvcuda` 一个全局符号——这是"绑定层不泄漏 C++ 符号"的打包纪律。
4. 执行：`python -m pytest` 跑整个目录；唯一的例外是 `test_nvtx_markers.py`，它需要 NVTX 探针注入，单独起一个 pytest 进程跑，避免探针污染整个套件。

日常开发则可以绕过包装脚本，直接在 `tests/cvcuda/python` 目录下运行 pytest（需要 cvcuda 已安装，且该目录在收集路径上以便 import 三个基础设施模块）。

#### 4.1.3 源码精读

测试目录的结构说明写在 tests/README.md 中：

- [tests/README.md:41-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L41-L56)：目录树注释，明确 `cvcuda/python/` 存放 "Python pytest tests (test_op*.py)"，并列出三套 requirements（common/numpy1/numpy2 按 CUDA 与 NumPy 版本组合）。
- [tests/README.md:33-39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L33-L39)：三步走——`bash build.sh -DBUILD_TESTS=1` 构建、`tests/install_test_dependencies.sh` 装依赖、`build-rel/bin/run_tests.sh` 运行。

包装脚本的关键段落：

- [tests/cvcuda/python/cvcuda_test_python.in:40-48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L40-L48)：声明依赖三元组 `pytest cupy numpy`，并逐版本探测可导入性，决定该版本是否参跑。
- [tests/cvcuda/python/cvcuda_test_python.in:98-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L98-L107)：用 `readelf -sWD` 列出 `_cvcuda` 模块的全局符号，若多于一个、或不是 `PyInit__cvcuda`，直接以退出码 3/4 失败——防止绑定库意外导出 C++ 符号破坏 ABI 卫生。
- [tests/cvcuda/python/cvcuda_test_python.in:75-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L75-L84)：注释解释为什么 NVTX 标记测试要隔离到独立进程——探针若贯穿整个套件，其记录缓冲会随每次算子调用无限增长，有耗尽主机内存的风险。
- [tests/cvcuda/python/cvcuda_test_python.in:111-117](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L111-L117)：两段 pytest 调用——主套件 `--ignore` 掉 NVTX 测试，随后带 `NVTX_INJECTION64_PATH` 单独再跑它。

#### 4.1.4 代码实践

1. **实践目标**：不跑全部 94 个文件，只针对一个算子运行测试，并学会按测试 ID 过滤。
2. **操作步骤**：
   ```bash
   cd tests/cvcuda/python
   pytest test_opflip.py -v                # 只跑 flip
   pytest test_opflip.py -v -k "varshape"  # 只跑变长批相关用例
   pytest test_opflip.py --collect-only -q | head -30   # 先看会展开成哪些用例
   ```
   若用构建产物运行：`build-rel/bin/run_tests.sh cvcuda,python`（标签过滤语法见 u7-l1）。
3. **需要观察的现象**：`-v` 输出中每个用例的 ID 形如 `test_op_flip[tau0-...]`、`flip_tensor-input[tensor-U8-NHWC-3ch-...]`——前一类来自手写参数化，后一类来自工厂生成（4.3 节详解）。
4. **预期结果**：全部通过；`--collect-only` 能看到几十上百个用例 ID。本讲义撰写环境无 GPU 与已安装的 cvcuda 包，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cvcuda_util.py` 不会被 pytest 误当成测试文件收集？
**答案**：pytest 默认只收集匹配 `test_*.py`（或 `*_test.py`）的文件及其内部 `test_*` 函数/类。`cvcuda_util.py` 文件名不匹配，它只是被测试文件 import 的普通模块。

**练习 2**：`cvcuda_test_python` 包装脚本在运行 pytest 之前做的符号检查，防的是什么问题？
**答案**：防止 `_cvcuda` 扩展模块导出 `PyInit__cvcuda` 之外的 C/C++ 符号。符号一旦泄漏，链接了不同版本 CV-CUDA 库的程序可能发生符号冲突或绑定到错误实现，破坏 ABI 卫生（对照 u6-l2 的符号版本机制）。

**练习 3**：`test_nvtx_markers.py` 为什么必须独占一个 pytest 进程？
**答案**：它依赖 `NVTX_INJECTION64_PATH` 注入探针，而 NVTX 只在进程启动时读取该变量一次；若让探针贯穿全套件，所有算子调用与 cupy 的 NVTX 区间都会被记录，缓冲区无界增长（只有该标记测试会重置它），有耗尽主机内存的风险。

### 4.2 手写算子测试：test_opflip.py 的两类断言

#### 4.2.1 概念说明

打开任何一个 `test_op*.py`，都会看到两种风格的测试并存：

- **手写测试**（`test_op_flip` / `test_op_flipvarshape`）：开发者逐行编写，覆盖 allocating 与 `_into` 两种变体，断言输出的**元数据契约**——shape、layout、dtype，或变长批的 `capacity/uniqueformat/maxsize`。
- **工厂生成测试**（文件末尾的 `make_op_tests` 调用，见 4.3 节）：声明式地给出支持矩阵，自动展开成大批正/负用例。

初学者最容易误解的一点：**手写测试不断言像素值**。`test_op_flip` 里没有一处 `assert_array_equal`。这不是疏漏，而是分工——逐字节的数值正确性由 C++ 系统测试的 CPU 黄金参考（u7-l1）保证；Python 测试关注的是 Python 绑定层特有的契约：重载决议是否选中了正确入口、输出对象是否被正确创建/复用、`_into` 是否原样返回 `dst`。

#### 4.2.2 核心流程

`test_op_flip` 的执行流程：

```
构造输入 Tensor(shape, dtype, layout)
  ├─ allocating 变体: out = cvcuda.flip(input, flip_code)
  │    断言 out.layout/shape/dtype 与 input 一致（flip 不改变任何元数据）
  └─ _into 变体: 预分配 out + 新建 stream
       tmp = cvcuda.flip_into(src=input, dst=out, flipCode=..., stream=stream)
       断言 tmp is out（原样返回 dst）
       再次断言三元元数据一致
```

`test_op_flipvarshape` 的流程多了一步"参数张量化"：变长批入口中，`flipCode` 不再是标量而是一个 `(N,1)` 的 int32 张量（NC 布局），每张图一个翻转码——这正是 u3-l1 讲过的"变长批入口把可变参数升级为张量"。

数据构造全部委托给 `cvcuda_util`：

```
util.create_image_batch(n, fmt, size, max_random, rng)
    循环 n 次: create_image((w,h), fmt, ...) → batch.pushback(img)
util.create_tensor((n,1), np.int32, "NC", ...)
    generate_data 随机数 → to_cuda_buffer(cupy 上传) → as_tensor 包装
```

#### 4.2.3 源码精读

Tensor 路径的手写测试——只查元数据，两变体都覆盖：

- [tests/cvcuda/python/test_opflip.py:52-71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py#L52-L71)：先 `cvcuda.flip(input, flip_code)` 后断言 `out.layout/shape/dtype` 与输入一致；再预分配输出并显式传 `stream=` 调 `flip_into`，断言 `tmp is out`。参数表（L27-51）刻意混排了 NHWC/HWC、uint8/float32/uint16/int32、flipCode 三种取值。
- [tests/cvcuda/python/test_opflip.py:114-142](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py#L114-L142)：变长批版本。`util.create_image_batch` 造随机图批，`util.create_tensor((num_images,1), np.int32, "NC", ...)` 造逐图翻转码；断言 `len/capacity/uniqueformat/maxsize` 四项批元数据。`_into` 路径用 `util.clone_image_batch(input)` 预备同构输出批。
- [tests/cvcuda/python/test_opflip.py:145-150](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py#L145-L150)：两个参数工厂。注意变长批版把 `flipCode` 造成长度为 2 的 "N" 张量——因为工厂生成的 `image_batch` 包装默认批大小就是 2（见 4.3.3）。

支撑它的数据构造工具：

- [tests/cvcuda/python/cvcuda_util.py:89-119](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L89-L119)：`generate_data` 按整数/实数类型分支生成随机数据；不传 `rng` 则生成全零——这也是很多"只测契约"的测试能容忍未初始化数据的原因。
- [tests/cvcuda/python/cvcuda_util.py:146-166](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L146-L166)：`to_cvcuda_tensor` 把 CPU/CUDA 数据经 cupy 上传后用 `as_tensor` 零拷贝包装，并处理"布局秩大于数据秩时左侧补 1"的对齐逻辑。
- [tests/cvcuda/python/cvcuda_util.py:306-337](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L306-L337)：`create_image_batch`，`size=(0,0)` 时每张图在 `[1, max_size]` 内随机取宽高——变长批的"变长"就是这么来的。
- [tests/cvcuda/python/cvcuda_util.py:340-356](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L340-L356)：`clone_image_batch`，只克隆外壳（尺寸+格式），不拷贝像素——正好用作 `_into` 的输出批。

#### 4.2.4 代码实践

1. **实践目标**：确认"手写测试无数值断言"这一判断，并数清参数化的规模。
2. **操作步骤**：
   ```bash
   grep -n "assert" tests/cvcuda/python/test_opflip.py        # 统计断言种类
   pytest tests/cvcuda/python/test_opflip.py --collect-only -q # 数一数用例数
   ```
3. **需要观察的现象**：断言全部是 `==`（元数据相等）与 `is`（对象同一性），没有 `np.testing.*`。
4. **预期结果**：`test_op_flip` 展开 5 个用例、`test_op_flipvarshape` 展开 5 个用例；再加上工厂生成的成百上千个（数量取决于支持矩阵与全集的乘积，见 4.3）。**待本地验证**（精确用例数取决于本机 cvcuda 版本）。

#### 4.2.5 小练习与答案

**练习 1**：`test_op_flip` 为什么坚持断言 `tmp is out` 而不是 `tmp == out`？
**答案**：`_into` 变体的契约是"写入调用者给定的 dst 并**原样返回同一个对象**"（u3-l3）。`is` 校验对象同一性，能捕捉"库偷偷新建了另一个张量返回"这类契约破坏；而 `==` 对 pybind11 对象未必有定义，也区分不出复用与新建。

**练习 2**：变长批测试里 `flipCode` 为什么是 `(num_images, 1)` 的张量而 Tensor 测试里是 Python 标量？
**答案**：Tensor 批的整批共享一个参数，标量即可；变长批入口支持逐图不同参数，因此参数被"张量化"为按 N 索引的张量（u3-l1 的四连函数套路）。

**练习 3**：`generate_data` 不传 `rng` 时生成全零数据，这些测试为什么还能通过？
**答案**：因为它们断言的是元数据契约（形状/布局/类型/不抛异常），与像素值无关；flip 对全零图和随机图产生的元数据结果相同。真正需要非平凡数据的是数值断言测试（见第 5 节综合实践）。

### 4.3 契约矩阵工厂：make_op_tests

#### 4.3.1 概念说明

`cvcuda_tools.make_op_tests` 是一个**测试生成器**：你声明"某算子支持哪些 dtype/layout/通道数（或图像格式）"，它自动生成一整批 pytest 用例：

- **正例**：支持集内的每个 (dtype × layout × channels) 组合，构造对应容器、调用算子、同步、不抛异常即通过。
- **负例**：全集减支持集得到的每个不支持值，配合"基石组合"（keystone）只变化一维，断言抛 `RuntimeError`（或调用方声明的异常类型）。

它的文档字符串里有一句关键的自我定位（意译）："这些生成的测试校验的是 **Python 侧声明的输入契约** 与算子实现中运行时守卫的一致性；它们不机器核对公开头文件文档，也**不能替代输出数值正确性测试**。" 换句话说，它测的是"支持矩阵表没写错、守卫真的会拦"，而不是"算出来的数对不对"。

工厂模式解决的是维护成本问题：61 个算子 × 每个数十种组合，手写不现实；而支持矩阵变了（比如新增 S16 支持），只需改一处集合声明，正负用例自动重新平衡。

#### 4.3.2 核心流程

生成逻辑的数学骨架只有三个差集：

- 不支持 dtype 集：\( D_{neg} = SCALAR\_TYPES\_SET \setminus D_{sup} \)
- 不支持布局集：\( L_{neg} = IMAGE\_LAYOUTS \setminus L_{sup} \)
- 不支持通道集：\( C_{neg} = CHANNELS \setminus C_{sup} \)

整体流程：

```
make_op_tests(name, runner_info, keystone_dlc, supported_*)
  ├─ 判定模式：给了 supported_formats → Format 模式；给了任一 dtype/layout/channels → DType 模式（两者可叠加）
  ├─ DType 模式必须提供 keystone_dlc（基石三元组），否则 ValueError
  ├─ 正例：itertools.product(runners × dtypes × layouts × channels × extra 组合)
  │     ├─ image/image_batch 包装：跳过无 Format 映射的 (dtype, channels)
  │     └─ 逐组合注册参数化测试 test_op_{name}_input
  ├─ 负例×3：以 keystone 为基线，一次只换一维为不支持值
  │     ├─ test_op_{name}_dtype_negative
  │     ├─ test_op_{name}_layout_negative（仅 tensor 类包装，图像无布局概念）
  │     └─ test_op_{name}_channels_negative
  └─ 返回 {测试名: 测试函数} 字典 → 调用方 globals().update(...) 注册
```

其中"runner"描述一种输入包装方式，`(wrapper, op, param_factory)` 三元组：wrapper 是 `"tensor"` / `"tensor_batch"` / `"image"` / `"image_batch"` 之一；op 是算子函数；param_factory 按 `(dtype, layout, channels)` 现算算子额外参数（如 flip 的 `flipCode` 张量）。

#### 4.3.3 源码精读

生成器的公共入口与模式判定：

- [tests/cvcuda/python/cvcuda_tools.py:562-578](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L562-L578)：`make_op_tests` 完整签名——`name`、`runner_info`、Format 模式的 `supported_formats`、DType 模式的 `keystone_dlc + supported_dtypes/layouts/channels`，以及排除与附加参数的微调旋钮。
- [tests/cvcuda/python/cvcuda_tools.py:592-596](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L592-L596)：文档中"只验证输入契约、不替代数值正确性测试"的边界声明，是理解整套生成测试定位的权威表述。
- [tests/cvcuda/python/cvcuda_tools.py:831-848](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L831-L848)：模式判定与约束——两种模式至少选一；DType 模式缺 `keystone_dlc` 直接抛 `ValueError`。

差集与正例展开的实现：

- [tests/cvcuda/python/cvcuda_tools.py:1017-1045](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L1017-L1045)：基石解包、三个"未指定则回退基石值"的默认、以及三个差集计算（`SCALAR_TYPES_SET - dtypes_to_test` 等）——上一节的公式就落在这几行。
- [tests/cvcuda/python/cvcuda_tools.py:1047-1082](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L1047-L1082)：正例的五重 `itertools.product` 循环；对 image 类包装用 `_has_format_mapping` 过滤掉无法表达为合法图像格式的组合（如 U8 的 5 通道），再经 `exclude_dlc` 模式剔除，最后注册为 `test_op_{name}_input`，测试 ID 形如 `tensor-U8-NHWC-3ch`。

执行器与断言原语：

- [tests/cvcuda/python/cvcuda_tools.py:108-126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L108-L126)：`_runner` 是所有用例的最终执行体：正例调用算子后 `cvcuda.Stream.current.sync()`（等当前流做完再判生死）；负例用 `pytest.raises` 包住，默认期待 `RuntimeError`——这正是 u6-l2 讲过的"NVCV 错误码在 Python 侧统一翻译为 RuntimeError"的落点。
- [tests/cvcuda/python/cvcuda_tools.py:80-105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L80-L105)：`_resolve_wrapper` 把包装名变成造数据的偏函数；`tensor_batch`/`image_batch` 的默认批大小 2 就写在这里（L95、L100-101）。
- [tests/cvcuda/python/cvcuda_tools.py:129-168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_tools.py#L129-L168)：`assert_dtypes` / `assert_layouts` 两个公开断言原语——逐值造容器、逐值调 `_runner`，单算子测试也可以直接手调它们。

全集常量来自 `cvcuda_types`：

- [tests/cvcuda/python/cvcuda_types.py:138-156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_types.py#L138-L156)：`SCALAR_FORMATS` 列出 11 种标量格式（U8…C128），派生出 `SCALAR_TYPES_SET`——dtype 负例的全集。
- [tests/cvcuda/python/cvcuda_types.py:588-604](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_types.py#L588-L604)：`IMAGE_LAYOUTS`（必须同时含 H、W 的 3D/4D 布局，注释解释了缺 H 会让图像算子崩溃）与 `CHANNELS = {1,2,3,4,5,6}`——布局与通道负例的全集。
- [tests/cvcuda/python/cvcuda_types.py:492-513](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_types.py#L492-L513)：`DTYPE_CHANNELS_TO_FORMAT`，image 包装过滤所依据的映射表。

test_opflip.py 底部的两次调用（这是"使用侧"的完整样例）：

- [tests/cvcuda/python/test_opflip.py:153-171](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py#L153-L171)：为 **Tensor 路径**生成 `flip_tensor_*` 系列测试：基石 `(U8, NHWC, 3ch)`，支持 dtype 不含 S16。
- [tests/cvcuda/python/test_opflip.py:173-190](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opflip.py#L173-L190)：为**变长批路径**生成 `flip_varshape_*` 系列：支持集多了 S16。L153-154 的注释点明了原因——Tensor 路径拒绝 S16，而变长批路径经 `flip_or_copy_var_shape` 仍支持它。同一个算子的两条入口有不同 dtype 契约，所以要分开声明两份矩阵。

#### 4.3.4 代码实践

1. **实践目标**：亲手推演一次差集生成，验证你理解了负例从哪来。
2. **操作步骤**：
   - 查 [tests/cvcuda/python/cvcuda_types.py:152-156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_types.py#L152-L156) 得到全集（11 种标量 dtype）。
   - 对 `flip_tensor` 的支持集 `{U8, U16, S32, F32}` 手算差集：应得 `{U32, S8, S16, F16, F64, C64, C128}` 共 7 个负例 dtype。
   - 运行 `pytest tests/cvcuda/python/test_opflip.py -v -k "flip_tensor_dtype_negative"` 对照。
3. **需要观察的现象**：每个负例的测试 ID 形如 `flip_tensor_dtype_negative[tensor-S16]`，且全部 PASSED（以"抛了 RuntimeError"的方式通过）。
4. **预期结果**：7 个 dtype 负例 × 1 个 tensor runner；再对 `flip_varshape` 重复一次，S16 应从负例中消失。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `keystone_dlc` 是 DType 模式的必填项？
**答案**：负例生成的策略是"以一个已知良好的组合为基线，一次只换一维为坏值"，否则无法区分失败到底由哪个维度引起。基石就是那个已知良好基线，缺了它负例无从构造（源码在 L844-848 直接抛 `ValueError`）。

**练习 2**：`layout_negative` 测试为什么要跳过 image/image_batch 包装？
**答案**：图像对象携带的是 `cvcuda.Format`（格式即隐含布局语义），没有独立的 layout 概念，传一个"不支持的布局"无从谈起；布局校验只对 tensor/tensor_batch 包装有意义（见 `_build_negative_test_params` 的 `skip_image_wrappers` 与 L597-600 的文档说明）。

**练习 3**：如果某算子新增了 F16 支持，测试要改几处？
**答案**：只需把 `cvcuda.Type.F16` 加进该算子 `make_op_tests` 调用的 `supported_dtypes` 集合。正例自动多一组用例，dtype 负例自动少一个——这正是声明式矩阵的价值。但注意：这不会自动带来数值正确性校验，C++ 侧的金标扩展（u7-l1 练习）仍需另行完成。

### 4.4 白盒窗口：cvcuda.internal 与 cvcuda._test

#### 4.4.1 概念说明

有些东西公开 API 观测不到：对象缓存当前占了多少字节、某个缓存条目自身占多少、辅助流是否排空、ResourceGuard 提交失败时清理路径是否正确……这些属于实现内部。CV-CUDA 的做法不是把内部函数混进公开命名空间，而是在 pybind11 模块初始化时挂两个**显式标注"仅测试用"的子模块**：

- `cvcuda.internal`：只读的白盒观测点（如 `nbytes_in_cache`、`syncAuxStream`）。
- `cvcuda._test`：主动制造故障的钩子（如让 ResourceGuard 的某个 C API 回调失败），用于测试清理与报错路径。

Main.cpp 里的注释把动机说得很直白：这保证了公开/私有 API 的清晰分离——用户被限制在公开 API 内，库作者因此可以随意重构私有 API 而不必担心弄坏用户代码。换句话说，**出现在 `internal`/`_test` 里的名字没有兼容性承诺**，测试代码可以依赖它，用户产品代码不应该。

#### 4.4.2 核心流程

一个白盒函数从 C++ 到 pytest 的路径：

```
Main.cpp: m.def_submodule("internal")            ← 模块初始化时先建子模块
Cache.cpp::Export: internal = m.attr(INTERNAL_SUBMODULE_NAME)
                   internal.def("nbytes_in_cache", ...)   ← 后到的 Export 往同一个子模块里挂函数
test_cache.py:     cvcuda.internal.nbytes_in_cache(img)   ← 测试直接调用
```

注意顺序依赖：`Main.cpp` 必须在任何 `Export*` 函数之前创建子模块，后者才能 `m.attr(...)` 取到它。这与 Main.cpp 中类型导出顺序的约束（基类先于派生类注册）同属一类"pybind11 注册顺序"问题。

`_test` 子模块的故障注入则反过来：测试先调 `cvcuda._test.fail_hold_resources(True)` 武装开关，让对应的 C API 回调走生产环境的失败分支，制造出"形状与真实失败完全一致"的错误，测完再解除武装。

#### 4.4.3 源码精读

子模块的创建与设计注释：

- [python/mod_cvcuda/Main.cpp:70-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L70-L84)：整段注释解释白盒子模块的存在意义（公开/私有分离、私有 API 可自由变更），并示范了其他 Export 函数如何取用子模块；L84 是 `m.def_submodule(INTERNAL_SUBMODULE_NAME)` 本体。
- [python/mod_cvcuda/nvcv/Definitions.hpp:23](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Definitions.hpp#L23)：`INTERNAL_SUBMODULE_NAME = "internal"`，常量定义保证各处拼写一致。
- [python/mod_cvcuda/Main.cpp:96-100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L96-L100)：类型导出顺序约束的注释（基类先注册、签名引用的类型先注册），这是理解 Main.cpp 排列逻辑的钥匙。

两个 internal 观测点的导出处：

- [python/mod_cvcuda/nvcv/Cache.cpp:503-508](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L503-L508)：`current_cache_size_inbytes` 是**公开**函数（当前设备缓存总字节）；紧接着 L507-508 取出 internal 子模块，挂上 `nbytes_in_cache(item)`（单个缓存条目的字节占用）——同为查询，一个面向用户、一个仅面向测试，分界就在这两行之间。
- [python/mod_cvcuda/nvcv/Stream.cpp:818-819](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L818-L819)：`internal.def("syncAuxStream", &SyncAuxStream)`，暴露辅助流同步（u4-l3 讲过辅助流负责保活跨流资源）。

`_test` 故障注入钩子：

- [python/mod_cvcuda/Main.cpp:118-127](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L118-L127)：创建 `_test` 子模块，注册 `resourceguard_destructor_error`（在 ResourceGuard 提交阶段注入一个 Python 运行时错误），并调用 `ExportCAPITestHooks`。
- [python/mod_cvcuda/nvcv/CAPI.cpp:62-76](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/CAPI.cpp#L62-L76)：`TestFailureInjection` 的注释块——三个确定性失败开关，让 C API 回调走"与真实失败形状完全一致"的生产失败路径；注释点名服务对象是 `tests/cvcuda/python/test_resourceguard.py`。
- [python/mod_cvcuda/nvcv/CAPI.cpp:775-780](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/CAPI.cpp#L775-L780)：`ExportCAPITestHooks` 本体，三个 `fail_*` 布尔开关的 Python 绑定。

test_cache.py 怎么用这些白盒点：

- [tests/cvcuda/python/test_cache.py:101-125](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L101-L125)：`test_cache_current_byte_size` 依次创建 Image、ImageBatchVarShape、Stream、Tensor、TensorBatch，每建一个就用 `cvcuda.internal.nbytes_in_cache(...)` 累加，断言 `current_cache_size_inbytes()` 与手工账本逐项相等——没有 `nbytes_in_cache` 这个白盒点，逐项核对根本写不出来。
- [tests/cvcuda/python/test_cache.py:146-170](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L146-L170)：`test_cache_limit_clearing` 验证三条淘汰规则：限额调小到低于当前占用→缓存清空；新条目大于限额→不进缓存；缓存涨到限额后再来一个→整体清空后收新条目。每一步都靠 `current_cache_size_inbytes()` 读出内部状态。
- [tests/cvcuda/python/test_cache.py:48-86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L48-L86)：`test_clear_cache_empties_gcbag` 的手法更巧——不直接观测内部结构，而是用 `sys.getrefcount` 监控被包装的 cupy 数组：包装张量持有它一份引用，`clear_cache()` 之后引用计数必须回落到初始值，从而**间接证明**缓存中的包装对象确实被销毁。这是 Python 白盒测试的另一个惯用技巧。
- [tests/cvcuda/python/test_cache.py:265-283](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L265-L283)：多 GPU 缓存测试用 `pytest.mark.skipif(NUM_GPUS < 2)` 跳过，并用 fixture 在每个用例后恢复设备号与各卡限额——白盒测试改动了内部状态，就必须自己负责复原。

#### 4.4.4 代码实践

1. **实践目标**：用 `cvcuda.internal.nbytes_in_cache` 亲手验证 u4-l2 的结论"包装对象不占缓存配额"。
2. **操作步骤**（示例代码，可在装好 cvcuda 与 cupy 的环境运行）：
   ```python
   import numpy as np, cupy, cvcuda

   cvcuda.clear_cache()
   a = cvcuda.Tensor((64, 64), np.uint8)            # 非包装对象：创建即入缓存
   print("created :", cvcuda.current_cache_size_inbytes(),
         cvcuda.internal.nbytes_in_cache(a))

   wrapped = cvcuda.as_tensor(cupy.zeros((64, 64), np.uint8), "HW")  # 包装对象
   cvcuda.median_blur(wrapped, [3, 3])               # 用一次算子使其进入 GCBag
   cvcuda.Stream.current.sync()
   print("wrapped :", cvcuda.current_cache_size_inbytes(),
         cvcuda.internal.nbytes_in_cache(wrapped))
   ```
3. **需要观察的现象**：第一行打印非零（Tensor 本体占缓存）；第二行的 `nbytes_in_cache(wrapped)` 应为 0，缓存总量不应因包装对象显著增长。
4. **预期结果**：与 u4-l2 讲的"包装对象零字节记账、入缓存仅为流保活"一致。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `nbytes_in_cache` 放在 `internal` 而 `current_cache_size_inbytes` 放在公开命名空间？
**答案**：缓存总量是用户做显存规划时合理需要的信息，属于产品承诺；"单个条目在缓存中占多少字节"依赖内部记账结构（CacheItem 的实现），库方保留重构自由，所以只对测试开放。

**练习 2**：`test_clear_cache_empties_gcbag` 没有用任何 `cvcuda.internal` 函数，它算白盒测试吗？
**答案**：算，只是"白"的层次不同——它不窥探 C++ 内部，而是利用 Python 的引用计数语义（`sys.getrefcount`）从外部推断内部对象生命周期。白盒的本质是"依据实现内部知识设计观测"，观测手段可以在任何一层。

**练习 3**：如果库作者把 `TestFailureInjection` 的三个开关改名，会破坏哪些代码？这为什么是可接受的？
**答案**：只会破坏 `tests/cvcuda/python/test_resourceguard.py` 等使用 `cvcuda._test.fail_*` 的仓库内测试，它们会随重构同步修改；因为 `_test` 从未向用户承诺兼容（Main.cpp L70-76 的注释），外部产品代码本就不该依赖它。这正是公开/私有分界的意义。

## 5. 综合实践

**任务**：为变长批 flip 写一个**带数值断言**的 Python 测试——填补 `test_opflip.py` 只测元数据的空缺，风格对齐官方的 `test_opremap.py::test_op_remapvarshape_content`。

**范本**：官方数值断言的写法在 [tests/cvcuda/python/test_opremap.py:246-272](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opremap.py#L246-L272)：numpy 造已知图 → `pushback(util.to_cvcuda_image(...))` 组批 → 算子 → `zip(b_src, b_dst)` 逐图 `src.cpu()` / `dst.cpu()` 读回 → `np.testing.assert_array_equal(a_dst, a_ref)`。

**步骤**：

1. 先运行现有测试建立基线：
   ```bash
   cd tests/cvcuda/python
   pytest test_opflip.py -v
   ```
2. 新建练习文件 `test_opflip_values_practice.py`（练习产物，不必提交仓库；若要提交须按仓库规范补 SPDX 头）。以下为**示例代码**：

   ```python
   # 示例代码：变长批 flip 的数值断言测试
   import numpy as np
   import pytest

   import cvcuda
   import cvcuda_util as util


   @pytest.mark.parametrize(
       "flip_code", [0, 1, -1]
   )
   def test_opflip_varshape_values(flip_code):
       # 1. 用 numpy 构造三张尺寸互不相同、像素值已知的 HWC 图
       sizes = [(4, 6), (5, 3), (2, 2)]  # (h, w)
       src_np = [
           np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3) for h, w in sizes
       ]

       # 2. 上传 GPU 并组成变长批（对照 test_opremap.py 的组批方式）
       b_src = cvcuda.ImageBatchVarShape(len(src_np))
       for img in src_np:
           b_src.pushback(util.to_cvcuda_image(img))

       # 3. 逐图同一翻转码，张量化为 "N" 布局的 int32 张量
       flipCode = util.to_cvcuda_tensor(
           np.full(len(src_np), flip_code, dtype=np.int32), "N"
       )

       # 4. 执行并同步
       b_dst = cvcuda.flip(b_src, flipCode)
       cvcuda.Stream.current.sync()

       # 5. 逐图读回 CPU，与 numpy 参考实现逐像素比对
       for src, dst, ref in zip(b_src, b_dst, src_np):
           if flip_code == 0:      # 上下翻
               expected = np.flip(ref, axis=0)
           elif flip_code > 0:     # 左右翻
               expected = np.flip(ref, axis=1)
           else:                   # 双轴翻
               expected = np.flip(ref, axis=(0, 1))
           np.testing.assert_array_equal(np.asarray(dst.cpu()), expected)
           np.testing.assert_array_equal(np.asarray(src.cpu()), ref)  # 输入不被改写
   ```

3. 运行：
   ```bash
   pytest test_opflip_values_practice.py -v
   ```

**需要观察的现象与预期结果**：

- 3 个参数各展开 1 个用例，全部 PASSED；每张不同尺寸的图都被独立翻转到正确方向，且输入批内容保持原样（算子无副作用）。
- 故意把 `expected` 的 `axis` 写反（比如 flip_code=0 时用 axis=1），测试应当立即失败——证明断言真的在比对像素而非恒真。
- 对照认知：`dst.cpu()`（[python/mod_cvcuda/nvcv/Image.cpp:1202-1204](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Image.cpp#L1202-L1204) 导出的方法）会把 GPU 数据同步拷回主机，所以断言前无需再手动同步图像；但算子提交后的 `Stream.current.sync()` 仍不可省（flip 是异步提交，u4-l1）。
- 本环境无 GPU，**待本地验证**。

**思考题**（选做）：把这个测试改造成"每张图不同翻转码"（把 `np.full` 换成 `np.array([1, 0, -1])`），期望值该怎么按图计算？答案提示：`zip(b_src, b_dst, src_np, codes)` 多zip一个码列表，按每图的码选 axis。

## 6. 本讲小结

- `tests/cvcuda/python` 是扁平 pytest 目录：每算子一个 `test_op*.py`（约 60 个）+ 三个基础设施模块（`cvcuda_util` 造数据、`cvcuda_tools` 造测试、`cvcuda_types` 存全集常量）+ 类型层/并发/缓存等专项测试；运行入口是 `run_tests.sh cvcuda,python` 或直接 pytest，包装脚本 `cvcuda_test_python` 额外做依赖探测与 `PyInit__cvcuda` 符号卫生检查。
- Python 手写算子测试断言的是**元数据契约**（shape/layout/dtype、批属性、`_into` 的对象同一性），数值正确性由 C++ 系统测试的 CPU 黄金参考负责——两套测试是互补分工而非重复。
- `make_op_tests` 是声明式契约矩阵工厂：给出基石三元组与支持集，正例由支持集的笛卡尔积展开，负例由全集差集 \( (D_{all} \setminus D_{sup}) \) 配合"一次只变一维"生成，经 `globals().update()` 注册为 pytest 用例；它只验证输入契约，不替代数值测试。
- 白盒观测点集中在 `cvcuda.internal`（`nbytes_in_cache`、`syncAuxStream`）与 `cvcuda._test`（ResourceGuard 故障注入开关）两个无兼容承诺的子模块，Main.cpp 中的创建顺序（先 `def_submodule`、后各 Export 挂载）是它们能工作的前提。
- 写数值断言的官方范本是 `test_opremap.py::test_op_remapvarshape_content`：numpy 已知输入 → `to_cvcuda_image` 组批 → 算子 → `zip` 逐图 `cpu()` 读回 → `np.testing.assert_array_equal`。

## 7. 下一步学习建议

- **u7-l3（基准测试）**：测试回答"对不对"之后，下一讲用 `bench/` 回答"快不快"；Python 基准同样依赖本讲的 util 基础设施思路。
- **u7-l4（NVTX 与性能分析）**：本讲提到的 `test_nvtx_markers.py` 与 NVTX 探针隔离机制，将在那一讲结合 Nsight Systems 展开。
- **延伸阅读源码**：`tests/cvcuda/python/test_resourceguard.py`（配合 `cvcuda._test` 的故障注入看清理路径测试怎么写）；`tests/cvcuda/python/test_multi_threading.py`（并发场景的测试组织，为 u4-l3 的概念提供断言证据）。
- 若你计划给某算子补 dtype 支持，按 u8-l1 的新算子清单走一遍：Python 侧改 `make_op_tests` 支持集 + 补数值断言，C++ 侧扩展金标与系统测试——本讲与 u7-l1 恰好覆盖这条链的两端。
