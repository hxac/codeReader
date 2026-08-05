# 环境准备与安装构建

## 1. 本讲目标

本讲解决一个最基础但至关重要的问题：**vllm-ascend 这个 Python 包是怎么从源码变成可运行插件的？** 学完后你应该能够：

1. 分清 vllm-ascend 的**编译期依赖**（编译 C++ 内核时需要的东西）和**运行期依赖**（实际跑推理时需要的东西），并说出 CANN、torch-npu、triton-ascend 各自在哪一层。
2. 认识 `envs.py` 里集中管理的构建环境变量，特别是 `SOC_VERSION`、`ASCEND_HOME_PATH`、`COMPILE_CUSTOM_KERNELS` 这三个，以及它们如何被「惰性求值」。
3. 跟着 `setup.py` 调用 CMake、`CMakeLists.txt` 收集内核源码、`build_aclnn.sh` 安装 ACLNN 自定义算子这条链路，看懂 C++ 自定义算子被编译进 `.so` 的全过程。
4. 知道在没有 NPU 的纯 CPU 环境（比如单元测试环境）下，为什么要设置 `COMPILE_CUSTOM_KERNELS=0` 才能正常安装。

本讲是动手实践的基础——理解了构建系统，后面所有讲义里引用的 `csrc/` 内核、`vllm_ascend_C` 模块才能落地。

## 2. 前置知识

阅读本讲前，建议你已经学过 **u1-l1（项目定位）** 和 **u1-l2（源码地图）**，至少知道：

- **vllm-ascend 是一个硬件插件**，通过 vLLM 的 `entry points` 机制被发现，不修改上游 vLLM 源码（见上一讲的「解耦」概念）。
- `vllm_ascend/` 是纯 Python 主体，`csrc/` 是 C++/AscendC 高性能内核。

此外需要几个通俗概念：

- **CANN（Compute Architecture for Neural Networks）**：华为昇腾的算子运行库与编译工具链，相当于 CUDA 生态里的「CUDA Toolkit + cuDNN」。它不是 pip 能装的，需要单独安装到机器上，默认路径是 `/usr/local/Ascend/ascend-toolkit/latest`。
- **torch-npu**：让 PyTorch 能调用 NPU 的适配层（类似 CUDA 版 PyTorch 里的 `torch.cuda`），是 pip 包。
- **AscendC**：昇腾提供的算子编程语言/框架，用来在 C++ 里写 NPU 内核，类比 CUDA 的 `__global__` kernel。
- **setuptools / CMake**：前者是 Python 打包标准工具，后者是 C/C++ 构建工具。vllm-ascend 用 setuptools 打 Python 包，再在 setuptools 内部调用 CMake 去 编译 C++ 内核。
- **SOC_VERSION**：芯片型号，如 `ascend910b1`（A2）、`ascend910_9391`（A3）、`ascend310p1`（310P）。不同芯片的指令集不同，内核要按目标芯片编译。

一句话：**vllm-ascend = 一个 Python 包 + 一堆按芯片型号编译的 C++/AscendC 内核**。本讲讲的就是「这两部分怎么一起被构建出来」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vllm_ascend/envs.py` | 集中管理所有 `VLLM_ASCEND_*` 及构建相关环境变量，用惰性求值（`__getattr__`）暴露为模块属性 |
| `requirements.txt` | 运行期 Python 依赖，精确锁版本（torch / torch-npu / triton-ascend 等） |
| `pyproject.toml` | 构建系统声明（`build-system.requires`）+ 工具配置（ruff/pytest/markdown），与 requirements 镜像 |
| `setup.py` | 打包入口：自动探测芯片型号、生成 `_build_info.py`、用自定义命令编排 CMake 与 ACLNN 编译 |
| `CMakeLists.txt` | C++/AscendC 内核的 CMake 构建：找依赖、收集内核源、编译 `vllm_ascend_C` 与 `vllm_ascend_kernels` |
| `csrc/build_aclnn.sh` | 按芯片型号分批构建并安装 ACLNN 自定义算子到 `vllm_ascend/_cann_ops_custom` |

记住一条主线：**`envs.py` 提供配置 → `setup.py` 读取配置并驱动 → `CMakeLists.txt` 与 `build_aclnn.sh` 真正编译**。

## 4. 核心概念与源码讲解

### 4.1 依赖体系：编译期与运行期

#### 4.1.1 概念说明

很多同学第一次看 vllm-ascend 会困惑：到底要装什么？这里要先分清两类依赖。

- **运行期依赖（runtime）**：真正跑推理时进程里必须存在的库。包括 Python 侧的 `torch`、`torch-npu`、`triton-ascend`、`transformers`，以及系统侧的 **CANN**（提供 `.so` 运行库和驱动）。
- **编译期依赖（build-time）**：只在「编译 C++/AscendC 内核」那一刻需要，编译完产物是 `.so`，之后运行只是加载它。包括 **CANN 工具链**（AscendC 编译器 `ascendc.cmake`）、CMake、pybind11、torch 的头文件等。

注意 CANN 比较特殊：它**同时**是运行期依赖（提供运行库）和编译期依赖（提供 AscendC 编译器）。这是 vllm-ascend 必须先装好 CANN 才能从源码构建的根本原因。

vllm-ascend 对版本要求**非常严格**，这是因为它和上游 vLLM、torch-npu、CANN 是「四方对齐」的：

- `requirements.txt` 里 `torch`、`torch-npu`、`triton-ascend` 都被精确 pin（用 `==`）。
- `CMakeLists.txt` 在编译期还会硬性校验 torch 主版本。

#### 4.1.2 核心流程

依赖安装的典型顺序：

```
1. 系统层：安装 CANN toolkit（设置好 ASCEND_HOME_PATH，默认 /usr/local/Ascend/ascend-toolkit/latest）
2. Python 层：pip install -r requirements.txt  （或随 setup.py 的 install_requires 自动装）
   └─ torch==2.10.0、torch-npu==2.10.0.post2、triton-ascend==3.2.1 ...
3. 源码层：pip install -e .  （触发 setup.py → CMake 编译 C++/AscendC 内核）
```

编译期，CMake 会去找 CANN 里的 AscendC 编译器；运行期，进程会去 CANN 的 `lib64` 里加载 `ascendcl`、`opapi` 等运行库。两者读的是同一个 `ASCEND_HOME_PATH`。

#### 4.1.3 源码精读

**运行期 Python 依赖（精确锁版本）**：[requirements.txt:L16-L20](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/requirements.txt#L16-L20) 把 `torch`、`torch-npu`、`torchvision`、`torchaudio`、`triton-ascend` 全部用 `==` 钉死。这说明 vllm-ascend 不能随意升级这些库，必须与 CANN/vLLM 版本对齐。

**编译期硬校验 torch 版本**：CMake 在配置阶段直接断言 PyTorch 必须是 `2.10.0`，否则 `FATAL_ERROR` 退出。[CMakeLists.txt:L22-L27](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L22-L27) 就是在做这件事——这能避免你用错误的 torch 头文件编出 ABI 不兼容的内核。

**构建系统声明镜像 requirements**：[pyproject.toml:L1-L34](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/pyproject.toml#L1-L34) 的 `build-system.requires` 与 `requirements.txt` 内容互为镜像。注意 `build-backend = "setuptools.build_meta"`，说明底层打包后端是 setuptools，`setup.py` 仍然是主入口（PEP 517 构建时会调用它）。

**CANN 同时作为编译期依赖**：[CMakeLists.txt:L41-L52](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L41-L52) 把 `ASCEND_HOME_PATH` 赋给 `ASCEND_CANN_PACKAGE_PATH`，并从 CANN 目录里找到 `ascendc_kernel_cmake`，再 `include(...ascendc.cmake)`。如果找不到 AscendC 编译器，就直接 `FATAL_ERROR`——这就是「没有 CANN 就编不出内核」的源头。

#### 4.1.4 代码实践

1. **实践目标**：搞清楚「这一行依赖到底什么时候用」。
2. **操作步骤**：
   - 打开 `requirements.txt`，找到 `torch-npu==2.10.0.post2` 和 `triton-ascend==3.2.1`。
   - 打开 `CMakeLists.txt` 第 22–27 行，看 torch 版本校验。
3. **需要观察的现象**：你会在两处都看到 `2.10.0` 这个版本号。
4. **预期结果**：能用自己的话回答「为什么我把 torch 升到 2.11，vllm-ascend 的 C++ 内核就编译不过」——因为 `CMakeLists.txt` 第 25 行 `VERSION_EQUAL "2.10.0"` 会失败，且运行期 `torch-npu` 也要求 torch 同版本。
5. 本步骤无需 NPU，**待本地验证**：你可以临时把 CMakeLists 里的校验值改一下看报错信息（仅阅读理解，不要提交修改）。

#### 4.1.5 小练习与答案

**练习 1**：CANN 为什么既是编译期依赖又是运行期依赖？
**参考答案**：编译期需要 CANN 里的 AscendC 编译器（`ascendc.cmake`）把 `.cpp` 内核编成目标芯片的机器码；运行期需要 CANN 的运行库（`ascendcl`、`opapi` 等）让进程加载并执行这些内核。

**练习 2**：如果你在 `requirements.txt` 里把 `triton-ascend` 删掉，安装还能成功吗？推理会受影响吗？
**参考答案**：安装大概率仍能成功（Python 依赖层面），但运行期依赖 Triton 的算子（见后续 u6-l2 Triton 算子集讲义）会在调用时 `ImportError`，因此不能删。

---

### 4.2 构建环境变量：envs.py

#### 4.2.1 概念说明

构建一个 vllm-ascend 包，光有依赖还不够，还要回答一堆「编译给哪块卡？」「CANN 装在哪？」「要不要编自定义内核？」「用几个线程编？」的问题。这些**构建参数**全部通过**环境变量**传入，而 `envs.py` 就是它们的统一登记处。

`envs.py` 的设计有两个值得学习的点：

1. **集中登记**：所有变量写在一个 `env_variables` 字典里，注释就是文档，还用 `# begin-env-vars-definition` / `# end-env-vars-definition` 标记给文档生成器抽取（这正是本讲能在 `envs.py` 顶部看到注释的原因）。
2. **惰性求值**：变量值不是模块加载时就算好，而是**第一次被访问时**才调用对应的 lambda 去读 `os.getenv`。这样既避免启动时无谓开销，也保证读取的是「此刻」的环境。

#### 4.2.2 核心流程

`envs.py` 的求值链路：

```
某处代码: envs.COMPILE_CUSTOM_KERNELS
   │
   ▼ 触发模块级 __getattr__("COMPILE_CUSTOM_KERNELS")
   │
   ▼ 在 env_variables 字典里查到 lambda
   │
   ▼ 执行 lambda: bool(int(os.getenv("COMPILE_CUSTOM_KERNELS", "1")))
   │
   ▼ 返回 True / False
```

关键环境变量速查表（构建相关）：

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `SOC_VERSION` | 无（自动探测） | 目标芯片型号，决定内核编译目标与 ACLNN 算子分支 |
| `ASCEND_HOME_PATH` | `/usr/local/Ascend/ascend-toolkit/latest` | CANN 安装路径，编译期与运行期都读 |
| `COMPILE_CUSTOM_KERNELS` | `1`（True） | 是否编译 C++/AscendC 内核；UT 无卡环境设为 `0` |
| `MAX_JOBS` | CPU 核数 | 编译并行度 |
| `CMAKE_BUILD_TYPE` | `Release` | `Release` / `Debug` / `RelWithDebugInfo` |
| `CXX_COMPILER` / `C_COMPILER` | 系统默认 | 指定编译器 |
| `VERBOSE` | `0` | 编译期打印详细日志 |
| `VLLM_ASCEND_ENABLE_BATCH_MEMCPY` | 自动探测 | 强制开关批量拷贝路径，会透传给 CMake |

#### 4.2.3 源码精读

**集中登记表**：[vllm_ascend/envs.py:L30-L103](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L30-L103) 是完整的 `env_variables` 字典。每个条目都是 `"名字": lambda: 读环境变量`。注意字典的 value 是「函数」而不是「值」，这是惰性求值的基础。

**核心开关 `COMPILE_CUSTOM_KERNELS`**：[vllm_ascend/envs.py:L38-L43](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L38-L43) 注释明确说：默认 `True`，**仅在没有 NPU 的 UT 场景才设为 `False`**，其他场景不要关。这对应本讲的实践任务。

**芯片型号 `SOC_VERSION`**：[vllm_ascend/envs.py:L50-L53](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L50-L53) 默认为空，留待 `setup.py` 用 `npu-smi` 自动探测（见 4.3 节）。

**CANN 路径 `ASCEND_HOME_PATH`**：[vllm_ascend/envs.py:L56-L58](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L56-L58) 给出默认值，与 `CMakeLists.txt` 里找 AscendC 编译器的路径一致。

**惰性求值实现**：[vllm_ascend/envs.py:L108-L112](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L108-L112) 定义了模块级 `__getattr__`：当你访问 `envs.XXX` 且 `XXX` 在字典里时，才调用对应 lambda。这正是「访问才求值」的机制，也意味着改了 `os.environ` 后再访问，能拿到新值。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证惰性求值行为。
2. **操作步骤**（示例代码，可在任意带 Python 的环境运行，无需 NPU）：

```python
# 示例代码：演示 envs.py 的惰性求值机制
import os
import vllm_ascend.envs as envs

# 第一次访问，读取当前环境
print("第1次 MAX_JOBS:", envs.MAX_JOBS)

# 改环境变量后再访问，观察是否拿到新值
os.environ["MAX_JOBS"] = "8"
print("第2次 MAX_JOBS:", envs.MAX_JOBS)   # 预期为 8，因为每次访问都重新求值
```

3. **需要观察的现象**：第 2 次打印应为 `8`。
4. **预期结果**：说明 `envs.MAX_JOBS` 不是模块加载时的快照，而是每次访问都重新读环境变量——这正是 `__getattr__` + lambda 带来的效果。
5. 若环境未装 vllm-ascend，可只读 `envs.py` 源码理解 `__getattr__`，标注**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `envs.py` 用 `lambda` 包一层，而不是直接 `os.getenv(...)`？
**参考答案**：为了惰性求值。如果直接写值，模块一 import 就会读环境变量，之后改 `os.environ` 不会生效；用 lambda + `__getattr__`，每次属性访问才执行 lambda，保证读到的是「当下」的环境。

**练习 2**：`COMPILE_CUSTOM_KERNELS` 的默认值是 `1`。在 UT 环境下你要怎么关掉它？为什么不能在所有环境都关？
**参考答案**：`export COMPILE_CUSTOM_KERNELS=0`（即设为 `0`）。因为关闭后不会编译 C++/AscendC 内核，UT 在无卡机器上跑不需要这些 `.so`；但在真实推理环境，内核是性能与功能的关键，关掉会导致算子缺失或回退到低效路径。

---

### 4.3 构建系统：setup.py 驱动 CMake

#### 4.3.1 概念说明

`setup.py` 是打包总指挥。它要做四件事：

1. **探测芯片**：如果 `SOC_VERSION` 没设，调用 `npu-smi` 自动识别；识别失败就报错并提示手动设置。
2. **生成构建信息**：把芯片型号翻译成「设备类型」（A2/A3/_310P/A5），写进 `vllm_ascend/_build_info.py`，供运行期分支使用。
3. **决定要不要编内核**：根据 `COMPILE_CUSTOM_KERNELS` 决定是否注册 C++ 扩展。
4. **编排编译**：通过自定义 setuptools 命令，先跑 `build_aclnn.sh` 安装 ACLNN 自定义算子，再用 CMake 编译 `vllm_ascend_C` 与 `vllm_ascend_kernels` 两个产物。

理解一个关键点：vllm-ascend 的 C++ 内核其实是**两套**：

- **AscendC 内核库 `vllm_ascend_kernels`**：由 `csrc/kernels/*.cpp` 等通过 `ascendc_library` 直接编译，是「自带的 AscendC 算子」。
- **ACLNN 自定义算子 `_cann_ops_custom`**：由 `build_aclnn.sh` 按 SOC 分批构建、安装，是「注册到 CANN 运行时的算子」。
- **pybind11 模块 `vllm_ascend_C`**：把 C++ 接口暴露给 Python 调用，链接上述两者与 CANN/torch-npu 库。

#### 4.3.2 核心流程

`pip install -e .` 触发后的整体流程（伪代码）：

```
setup.py 启动
 ├─ load envs.py（拿到 COMPILE_CUSTOM_KERNELS / SOC_VERSION / ...）
 ├─ if SOC_VERSION 为空:
 │     get_chip_type()  # 调 npu-smi 探测；失败则抛错要求手动设置
 ├─ gen_build_info()    # 写 _build_info.py: __device_type__ = 'A2'/'A3'/...
 ├─ cmdclass 注册:
 │     develop / build_py → 先 gen_build_info
 │     build_ext → cmake_build_ext
 │     build_aclnn → 跑 build_aclnn.sh
 │     install → 先 build_ext 再 install
 └─ setup(...) ext_modules:
       if COMPILE_CUSTOM_KERNELS:
           [CMakeExtension("vllm_ascend.vllm_ascend_C")]
       else:
           []   # 不编内核

# 当 build_ext 执行（cmake_build_ext.run）：
 ├─ if COMPILE_CUSTOM_KERNELS: 先 run_command("build_aclnn")
 └─ super().run() → build_extensions()
      ├─ if not COMPILE_CUSTOM_KERNELS: 直接 return  # 跳过内核编译
      ├─ configure(): 组装 cmake 参数（ASCEND_HOME_PATH/SOC_VERSION/TORCH_NPU_PATH...）
      └─ cmake --build / --install

# CMakeLists.txt 配置阶段：
 ├─ find_package(Torch) + 校验 2.10.0
 ├─ 找 ascendc.cmake（来自 CANN）
 ├─ glob csrc/kernels/*.cpp
 ├─ if 310P 或 950: 跳过 vllm_ascend_kernels 编译
 ├─ pybind11_add_module(vllm_ascend_C ...)
 ├─ 探测/强制 batch_memcpy 宏
 └─ install vllm_ascend_C [、vllm_ascend_kernels]
```

#### 4.3.3 源码精读

**加载 envs 模块**：[setup.py:L135](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L135) 用 `load_module_from_path` 直接从路径加载 `envs.py`，这样在 setup 早期（还没装好包）就能读取构建参数。

**自动探测芯片型号**：[setup.py:L74-L132](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L74-L132) 的 `get_chip_type()` 调用 `npu-smi info` 解析 `Chip Name` / `Chip Type` / `NPU Name`，按 310/910/950 等规则拼出 `SOC_VERSION`。注意它在 `npu-smi` 不存在时只警告并返回空字符串（[setup.py:L128-L132](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L128-L132)）。

**无卡时的友好报错**：[setup.py:L137-L150](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L137-L150) 在探测失败时明确提示「这可能是 CPU-only 环境」，并列出各型号应设的 `SOC_VERSION` 示例。这是你在纯 CPU 环境 `pip install` 会看到的那段错误。

**生成 `_build_info.py`**：[setup.py:L153-L193](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L153-L193) 用一张 `soc_to_device` 映射表把 `SOC_VERSION` 翻译成 `A2`/`A3`/`_310P`/`A5`，写入运行期读取的 `__device_type__`。这就是「编译期确定的设备类型在运行期被分支代码使用」的桥梁。

**自定义命令编排**：[setup.py:L504-L510](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L504-L510) 的 `cmdclass` 把默认的 `develop`/`build_py`/`build_ext`/`install` 替换为自定义版本，并新增 `build_aclnn` 命令。

**`COMPILE_CUSTOM_KERNELS` 的两个拦截点**：
- 扩展列表只在开启时才注册：[setup.py:L462-L464](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L462-L464) 决定 `ext_modules` 是否包含 `CMakeExtension`。
- 真正编译时再次检查：[setup.py:L368-L370](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L368-L370) 的 `build_extensions` 在关闭时直接 `return`，连 CMake 都不调用。

**先编 ACLNN 再编扩展**：[setup.py:L439-L445](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L439-L445) 的 `cmake_build_ext.run` 在开启内核编译时，先 `run_command("build_aclnn")`，再做标准 `build_ext`。`build_and_install_aclnn`（[setup.py:L214-L231](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L214-L231)）就是去执行 `csrc/build_aclnn.sh`。

**组装 CMake 参数**：[setup.py:L262-L366](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L262-L366) 的 `configure()` 把 `ASCEND_HOME_PATH`、`PYTHON_EXECUTABLE`、pybind11 路径、`TORCH_NPU_PATH`、`SOC_VERSION`、`VLLM_ASCEND_ENABLE_BATCH_MEMCPY` 等全部转成 `-D...=...` 传给 CMake。其中 `check_or_set_default_env`（[setup.py:L47-L63](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L47-L63)）还负责把 `ASCEND_HOME_PATH` 写回 `os.environ`，因为 CANN 包会在 CMake 里再读一次这个变量。

**CMake 收集并编译内核**：
- 找 AscendC 编译器：[CMakeLists.txt:L42-L52](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L42-L52)
- 收集自带内核源码：[CMakeLists.txt:L54-L61](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L54-L61)
- **310P/950 跳过 `vllm_ascend_kernels`**：[CMakeLists.txt:L74-L80](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L74-L80)，这是芯片能力差异带来的分支（950 还会排除 MLAPO 与 batch_matmul_transpose，见 [CMakeLists.txt:L63-L72](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L63-L72)）。
- 生成 pybind11 模块：[CMakeLists.txt:L111](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L111)
- 探测 batch_memcpy 能力：[CMakeLists.txt:L113-L153](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L113-L153)（CANN 8.5+ 才有 `aclrtMemcpyBatchAsync`，否则回退到循环拷贝）。
- 310P 专属宏：[CMakeLists.txt:L155-L161](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L155-L161) 定义 `ASCEND_PLATFORM_310P` 与（非 310P/950 的）`VLLM_ENABLE_ATB_AND_DIRECT_KERNELS`。
- 链接 CANN/torch-npu 库：[CMakeLists.txt:L171-L196](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L171-L196)，可见 `ascendcl`、`opapi`、`torch_npu` 等都是运行期要加载的库。

**ACLNN 按芯片分批构建**：[csrc/build_aclnn.sh:L78-L224](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/csrc/build_aclnn.sh#L78-L224) 根据 `SOC_VERSION` 匹配 `ascend310` / `ascend910b` / `ascend910_93` / `ascend950` 四个分支，每个分支维护一份 `CUSTOM_OPS_ARRAY`，再调用 CANN 的 `build.sh` 编译并安装到 `vllm_ascend/_cann_ops_custom`（[csrc/build_aclnn.sh:L256-L282](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/csrc/build_aclnn.sh#L256-L282)）。不同芯片支持的算子集合不同，这就是为什么要按型号分批。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：在无 NPU 的 UT 环境下，说明要设置哪些环境变量才能跳过自定义内核编译，并解释原因。

1. **操作步骤**：
   - 在你的 shell 里设置：

     ```bash
     # 跳过 C++/AscendC 内核与 ACLNN 算子编译
     export COMPILE_CUSTOM_KERNELS=0
     # CPU-only 机器无法用 npu-smi 探测，需要显式给一个 SOC_VERSION（仅用于让 setup.py 不报错）
     export SOC_VERSION="ascend910b1"
     # 如果你的 CANN 不在默认路径，还要指定（UT 环境通常无 CANN，配合上面跳过编译即可）
     # export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest
     ```
   - 然后执行安装（editable 模式）：

     ```bash
     pip install -e .
     ```
2. **需要观察的现象**：
   - 安装过程**不会**调用 `cmake`、不会跑 `build_aclnn.sh`，也不会尝试找 `npu-smi` 之外的 CANN 编译器。
   - 控制台可能看到 `Generated _build_info.py with SOC version: ascend910b1` 之类的日志。
3. **预期结果**：包能成功安装，`import vllm_ascend` 可用，但 `vllm_ascend_C` 这个 C++ 扩展不存在（因为没编）。UT 因为不依赖真实内核，可以照常运行。
4. **为什么**：把上面的设置对应到源码——
   - `COMPILE_CUSTOM_KERNELS=0` 让 [setup.py:L462-L464](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L462-L464) 的 `ext_modules` 为空，且 [setup.py:L368-L370](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L368-L370) 的 `build_extensions` 直接 `return`，从而跳过整个 CMake 与 ACLNN 流程，也就不需要真实 CANN 编译器。
   - 仍需 `SOC_VERSION`，因为 [setup.py:L137-L150](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L137-L150) 在探测失败时会硬性报错；给它一个值只是为了绕过探测，并不触发编译。
5. 本实践**待本地验证**：实际能否成功取决于 UT 环境是否已装好 `torch`/`torch-npu`/`triton-ascend` 等运行期依赖。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `build_aclnn.sh` 要按 `SOC_VERSION` 分四个分支？
**参考答案**：不同芯片（310P / A2(910b) / A3(910_93) / A5(950)）支持的 AscendC 算子集合不同，`CUSTOM_OPS_ARRAY` 各自维护。按型号分批能保证只编译该芯片真正能跑的算子，避免编出不兼容的产物。

**练习 2**：`pip install -e .`（editable）和 `pip install .`（非 editable）在 vllm-ascend 里对 `.so` 的处理有什么不同？
**参考答案**：从 [setup.py:L416-L426](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L416-L426) 可见，editable 模式（`develop`）下，编译出的 `.so` 会被 `shutil.copy` 回 `vllm_ascend/` 源码目录，这样你改源码后无需重新打包就能直接 `import`；非 editable 则安装到 `build_lib` 目标位置。

**练习 3**：310P 和 950 为什么在 CMakeLists 里跳过 `vllm_ascend_kernels` 编译？
**参考答案**：见 [CMakeLists.txt:L74-L80](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/CMakeLists.txt#L74-L80)。这两类芯片的能力与 A2/A3 不同，自带 AscendC 内核库暂不适用，因此只构建 `vllm_ascend_C`，相关算子改由 ACLNN 路径或其他方式提供（详见后续 u11-l2 310P 适配讲义）。

## 5. 综合实践

**任务**：为一张「Atlas A2」机器，从零写一份构建用的环境变量脚本 `build_env.sh`（本讲只产出脚本内容，不真正执行），并预测 `pip install -e .` 会触发哪些阶段。

要求你的脚本至少包含：`SOC_VERSION`、`ASCEND_HOME_PATH`、`COMPILE_CUSTOM_KERNELS`、`MAX_JOBS` 四项，并写明每一项取值的理由。

参考答案（示例脚本，非项目原有文件）：

```bash
# 示例代码：Atlas A2 构建环境脚本（仅供学习）
export SOC_VERSION="ascend910b1"          # A2 对应型号，见 setup.py 报错提示与 Dockerfile
export ASCEND_HOME_PATH="/usr/local/Ascend/ascend-toolkit/latest"  # CANN 默认安装路径
export COMPILE_CUSTOM_KERNELS=1           # 真实推理环境必须开启，编译 AscendC/ACLNN 内核
export MAX_JOBS=16                         # 按机器核数调整，控制编译并行度
```

预测的触发阶段（结合 4.3.2 流程图）：
1. `setup.py` 读 `envs.py`，发现 `SOC_VERSION` 已设，跳过 `npu-smi` 探测。
2. `gen_build_info()` 把 `ascend910b1` 翻译成 `A2`，写入 `_build_info.py`。
3. 因为 `COMPILE_CUSTOM_KERNELS=1`，`ext_modules` 注册 `CMakeExtension`。
4. `build_ext` 先 `run_command("build_aclnn")` → 走 `build_aclnn.sh` 的 `ascend910b` 分支，构建该芯片的 ACLNN 算子并安装到 `_cann_ops_custom`。
5. CMake 配置 + 编译：校验 torch 2.10.0、找 AscendC 编译器、编译 `vllm_ascend_kernels` 与 `vllm_ascend_C`、链接 CANN/torch-npu 库。
6. editable 模式下，`.so` 拷回 `vllm_ascend/` 源码目录。

> 本综合实践无需 NPU 也能完成「写脚本 + 预测阶段」部分；真实编译**待本地验证**。

## 6. 本讲小结

- vllm-ascend 的依赖分**编译期**（CANN 工具链/AscendC 编译器、CMake、pybind11）和**运行期**（torch、torch-npu、triton-ascend、CANN 运行库），CANN 兼具两者，且版本必须与上游 vLLM 四方对齐。
- 所有构建参数集中在 `vllm_ascend/envs.py` 的 `env_variables` 字典，采用 lambda + 模块级 `__getattr__` 实现**惰性求值**。
- 三个最关键的构建变量：`SOC_VERSION`（目标芯片）、`ASCEND_HOME_PATH`（CANN 路径）、`COMPILE_CUSTOM_KERNELS`（是否编内核）。
- `setup.py` 是总指挥：自动探测芯片 → 生成 `_build_info.py` → 按 `COMPILE_CUSTOM_KERNELS` 决定是否注册扩展 → 先 `build_aclnn.sh` 再 CMake 编译。
- C++ 内核有两套：自带 AscendC 库 `vllm_ascend_kernels`（310P/950 跳过）和 ACLNN 自定义算子 `_cann_ops_custom`（按芯片分批构建），外加 pybind11 模块 `vllm_ascend_C` 把它们暴露给 Python。
- 在无 NPU 的 UT 环境，设 `COMPILE_CUSTOM_KERNELS=0` 并给一个 `SOC_VERSION` 即可跳过内核编译，因为 `build_extensions` 会直接 `return`。

## 7. 下一步学习建议

- 下一讲 **u1-l4 快速上手：运行第一个推理** 会用 `examples/offline_inference_npu.py` 把本讲构建出的包真正跑起来，建议先完成本讲的安装实践再继续。
- 想深入「C++/AscendC 内核」细节，可跳到 **u6-l3 C++/AscendC 内核与构建**，它会展开 `csrc/` 下 `op_host`/`op_kernel` 的双文件结构与 AscendC 编程模型。
- 想了解芯片型号如何驱动运行期分支，可看 **u11-l2 Ascend 310P 适配**，它讲解了 `is_310p()` 与 `ASCEND_PLATFORM_310P` 宏的联动。
- 继续阅读源码建议：`setup.py` 的 `cmdclass`、`CMakeLists.txt` 的 SOC 分支、`csrc/build_aclnn.sh` 的算子清单，这三处是理解「构建产物从哪来」的核心。
