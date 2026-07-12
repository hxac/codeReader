# 安装与构建方式

## 1. 本讲目标

本讲承接 [u1-l2 目录结构与架构全景](u1-l2-directory-and-architecture.md)。你已经知道 lmdeploy 是「压缩—推理—服务」一体化工具包，源码分在 `lmdeploy/`（Python）和 `src/`（TurboMind 的 C++）两处。本讲回答一个最实际的问题:**这套东西到底怎么装到机器上、怎么从源码编译出来。**

学完本讲，你应该能够：

1. 说清楚 **普通用户安装**（`pip install lmdeploy`）与 **从源码构建**（`setup.py` + CMake）两条路线的区别与各自适用场景。
2. 看懂 lmdeploy 为什么要把依赖 **按设备拆分** 成 `requirements_cuda.txt` / `requirements_ascend.txt` / `requirements_rocm.txt` 等多套，并知道它们如何被 `setup.py` 拼装起来。
3. 理解 `setup.py` 如何用 `LMDEPLOY_TARGET_DEVICE`、`DISABLE_TURBOMIND`、`CUDACXX`、`CMAKE_BUILD_TYPE` 等环境变量决定 **是否编译 TurboMind C++ 扩展**。
4. 读懂顶层 `CMakeLists.txt` 的职责：它定义了 TurboMind 的 C++/CUDA 构建规则、拉取第三方库、选定 GPU 算力架构，并把编译产物 `_turbomind` 安装到 `lmdeploy/lib/`。

本讲只覆盖「安装与构建」这一层，不展开 TurboMind C++ 内部实现（那是 U6 的事）。

## 2. 前置知识

- **pip 与 wheel**:`pip install 包名` 会从 PyPI 下载预编译好的 wheel 文件并安装。wheel 是针对特定操作系统、Python 版本、有时还针对特定 CUDA 版本编译的二进制包,装起来快、不用本地编译。
- **setup.py 与可编辑安装**:`pip install -e .`(editable install)会把当前目录以「开发模式」装上,源码改动立即生效,适合贡献者。`setup.py` 是 Python 打包脚本,`pip` 通过它知道包名、版本、依赖、扩展模块怎么编译。
- **CMake**:C/C++/CUDA 项目的构建系统,作用类似 Python 里的 `setup.py`。它读 `CMakeLists.txt`,根据其中的规则调用编译器(nvcc/g++/MSVC)生成 `.so`/`.dll`/`.pyd`。
- **pybind11**:把 C++ 函数/类暴露给 Python 调用的库。TurboMind 用它把 C++ 引擎编译成一个 `_turbomind` 扩展模块,Python 端就能 `import` 并调用。
- **CUDA 与 GPU 算力架构(compute capability)**:NVIDIA 每代 GPU 有一个「算力版本号」,如 A100=80、H100=90a、RTX 5090=120a。nvcc 编译 CUDA 代码时要指定为哪些架构生成代码。
- **前置认知**:本讲默认你读过 [u1-l1](u1-l1-project-overview.md) 与 [u1-l2](u1-l2-directory-and-architecture.md),知道 lmdeploy 有 **PyTorch 引擎**(纯 Python,装好依赖就能跑)与 **TurboMind 引擎**(C++,需要编译)两套后端。

> 关键直觉:PyTorch 后端几乎「全是 Python 依赖」,装起来轻;TurboMind 后端要「编译 C++/CUDA 代码」,装起来重、对工具链(nvcc、CMake)有要求。理解了这一点,就理解了本讲全部环境变量存在的意义——它们大多是在控制 **要不要、以及怎么 编译 TurboMind**。

## 3. 本讲源码地图

本讲围绕「安装与构建」这条线,涉及的关键文件如下:

| 文件 | 作用 |
| --- | --- |
| [`setup.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py) | Python 打包入口。读设备环境变量、解析 requirements、决定是否编译 TurboMind 扩展、调用 `setup()`。 |
| [`requirements_cuda.txt`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements_cuda.txt) | CUDA 设备的「全套」依赖清单(对应 `[all]` extra),聚合 build + runtime + lite + serve。 |
| [`requirements_ascend.txt`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements_ascend.txt) | 华为昇腾(Ascend/NPU)设备的全套依赖清单,结构与 cuda 版对称。 |
| [`requirements/runtime_cuda.txt`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt) | CUDA 运行时基础依赖(对应默认 `install_requires`),含 torch/triton 等。 |
| [`requirements/build.txt`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/build.txt) | 构建期依赖(cmake_build_extension、pybind11、setuptools)。 |
| [`CMakeLists.txt`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt) | TurboMind 的 C++/CUDA 顶层构建脚本。拉取第三方库、设定 GPU 架构、产出 `_turbomind`。 |
| [`README.md`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md) | 给普通用户的安装说明(`pip install lmdeploy`)。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:

1. **按设备拆分的 requirements**——理解依赖矩阵的组织方式。
2. **setup.py 构建入口**——理解 Python 打包脚本如何读取设备变量、拼装依赖、决定是否编译扩展。
3. **CMakeLists.txt 与 TurboMind C++ 扩展构建**——理解 C++/CUDA 那一半是怎么被编译出来的。

---

### 4.1 按设备拆分的 requirements

#### 4.1.1 概念说明

lmdeploy 是 **跨设备** 的:同一个 Python 包,既能在 NVIDIA GPU(cuda)上跑,也能在华为昇腾 NPU(ascend)、AMD GPU(rocm)、摩尔线程(maca)、寒武纪(camb)等设备上跑。但不同设备的 **运行时依赖完全不同**:

- cuda 设备要 `torch` + CUDA 版 `triton` + NCCL;
- ascend 设备要 `torch-npu` + `dlinfer-ascend`,**不要** triton;
- rocm 设备的 torch 是 ROCm 版的。

如果把所有设备的依赖都塞进一个 `requirements.txt`,普通用户 `pip install` 时会装上一堆自己设备根本用不上的包,甚至版本冲突。所以 lmdeploy 的做法是:**按设备拆成多套 requirements,安装时只加载对应设备的那一套。**

#### 4.1.2 核心流程

依赖文件分布在两层:**仓库根目录**的 `requirements_<device>.txt` 与 **`requirements/` 子目录**里的细分文件。

```text
仓库根目录(对应 [all] extra,全套)
├── requirements_cuda.txt        ──┐
├── requirements_ascend.txt       ─┤  每个设备一个「聚合文件」,
├── requirements_rocm.txt          ├─┤  内容都是 -r 引用下面四类
├── requirements_maca.txt          │ │
└── requirements_camb.txt         ──┘ │
                                     │
requirements/(对应 install_requires 与各 extra) ◄┘
├── common.txt          通用依赖(fastapi/numpy/transformers...)
├── runtime_cuda.txt    cuda 运行时(torch/triton/...)  ◄── install_requires
├── runtime_ascend.txt  ascend 运行时(torch-npu/...)
├── runtime_rocm.txt    rocm 运行时
├── runtime_maca.txt    maca 运行时
├── runtime_camb.txt    camb 运行时
├── build.txt           构建期(cmake_build_extension/pybind11)  ◄── setup_requires
├── lite.txt            量化相关(对应 [lite] extra)
├── serve.txt           服务相关(对应 [serve] extra)
├── docs.txt            文档构建
├── readthedocs.txt     ReadTheDocs 用
└── test.txt            测试用(对应 tests_require)
```

根目录的聚合文件内容很简单,以 cuda 为例:

```text
# requirements_cuda.txt（根目录，[all] extra）
-r requirements/build.txt
-r requirements/runtime_cuda.txt
-r requirements/lite.txt
-r requirements/serve.txt
```

也就是说,`requirements_cuda.txt` 只是一个「指针清单」,用 `-r` 把 build、runtime_cuda、lite、serve 四块拼起来。`requirements_ascend.txt` 结构完全对称,只是把中间那行换成 `runtime_ascend.txt`。

#### 4.1.3 源码精读

先看根目录聚合文件本身——它就是四行 `-r` 引用([requirements_cuda.txt:1-4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements_cuda.txt#L1-L4)):

```text
-r requirements/build.txt
-r requirements/runtime_cuda.txt
-r requirements/lite.txt
-r requirements/serve.txt
```

这四行里的 `-r` 是 pip 的标准语法:「把另一个文件的内容也包含进来」。`requirements_ascend.txt` 与之完全对称,只是第二行换成 `runtime_ascend.txt`([requirements_ascend.txt:1-4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements_ascend.txt#L1-L4))。

再看 cuda 运行时依赖,这是默认安装时装进来的核心部分([requirements/runtime_cuda.txt:3-13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt#L3-L13)):

```text
accelerate>=0.29.3
aiohttp
apache-tvm-ffi==0.1.11; sys_platform == "linux" and "aarch64" not in platform_machine ...
flash-linear-attention
opencv-python-headless
peft<=0.14.0
prometheus_client
tilelang==0.1.11; sys_platform == "linux" and ...
torch<=2.10.0,>=2.0.0
torchvision<=0.25.0,>=0.15.0
triton<=3.6.0,>=3.0.0; sys_platform == "linux" and ...
```

注意三个细节:

1. 第一行是 `-r common.txt`([runtime_cuda.txt:1](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt#L1)),把通用依赖(fastapi、numpy、transformers、sentencepiece 等共 24 个,见 [common.txt](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/common.txt))合并进来。这样所有设备共享同一份通用清单,避免重复维护。
2. 行尾的 `; sys_platform == "linux" and ...` 是 **环境标记(environment marker)**:`triton`、`tilelang`、`apache-tvm-ffi` 这三个只在 **Linux x86_64** 上安装,在 aarch64(如部分 Jetson)或 Windows 上会自动跳过——因为这些包没有对应平台的 wheel。
3. 对比 ascend 运行时([runtime_ascend.txt:5-10](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_ascend.txt#L5-L10)),它装的是 `torch-npu` 和 `dlinfer-ascend`,**没有 triton**——这就是按设备拆分的价值所在。

> 速查表:常见 requirements 文件对应的安装场景
>
> | 文件 | 谁会装它 | 何时装 |
> | --- | --- | --- |
> | `requirements/runtime_<device>.txt` | 所有用户 | `pip install lmdeploy`(默认 `install_requires`) |
> | `requirements_<device>.txt`(根) | 需要全套的开发者 | `pip install lmdeploy[all]` |
> | `requirements/lite.txt` | 做量化的用户 | `pip install lmdeploy[lite]` |
> | `requirements/serve.txt` | 起服务的用户 | `pip install lmdeploy[serve]` |
> | `requirements/build.txt` | 从源码编译者 | 编译 TurboMind 时(`setup_requires`) |

#### 4.1.4 代码实践

**实践目标**:亲手看清 requirements 是怎么「分层聚合」的。

**操作步骤**:

1. 在仓库根目录打开 `requirements_cuda.txt`,数一数它有几行、每行引用了哪个文件。
2. 打开 `requirements/runtime_cuda.txt`,找到 `torch`、`triton`、`accelerate` 三个依赖的版本约束。
3. 打开 `requirements/runtime_ascend.txt`,确认它 **没有** `triton`,而是有 `torch-npu`。

**需要观察的现象**:

- `requirements_cuda.txt` 本身不含任何具体包名,全是 `-r` 引用。
- cuda 版的 `triton` 行带 `; sys_platform == "linux" ...` 标记,ascend 版根本没有这一行。

**预期结果**:你能在 1 分钟内口述「为什么 lmdeploy 要把 requirements 按设备拆开」。

#### 4.1.5 小练习与答案

**练习 1**:如果不按设备拆分,把 cuda 和 ascend 的依赖写进同一个 `requirements.txt`,会发生什么问题?

> **参考答案**:用户 `pip install` 时会同时拉取 `triton`(cuda 专用)和 `torch-npu`(ascend 专用),在 cuda 机器上 `torch-npu` 装不上或无意义,在 ascend 机器上 `triton` 无对应 wheel 导致报错。按设备拆分让每个设备只装自己需要的运行时。

**练习 2**:`requirements/runtime_cuda.txt` 第 1 行的 `-r common.txt` 起什么作用?为什么不直接把 common.txt 的内容复制进每个 runtime 文件?

> **参考答案**:合并通用依赖清单。所有设备共享同一份通用依赖(fastapi、transformers、numpy 等),用 `-r` 引用可避免在 5 个 runtime 文件里重复维护这 20 多个包,减少不一致风险。

---

### 4.2 setup.py 构建入口

#### 4.2.1 概念说明

`setup.py` 是 Python 打包的「总指挥」。lmdeploy 的 `setup.py` 干三件事:

1. **识别目标设备**:读 `LMDEPLOY_TARGET_DEVICE` 环境变量,决定按哪套 requirements 装依赖。
2. **决定是否编译 TurboMind**:只有 cuda 设备且未设置 `DISABLE_TURBOMIND` 时,才编译 C++ 扩展。
3. **把以上结果交给 `setup()`**:声明包名、版本、依赖、扩展模块、命令行入口。

理解 `setup.py` 的关键,是抓住那个「是否编译 TurboMind」的开关——它直接决定了安装是「轻」(纯 Python,几分钟)还是「重」(编译 C++/CUDA,可能几十分钟)。

#### 4.2.2 核心流程

`setup.py` 执行时的判定流程:

```text
读 LMDEPLOY_TARGET_DEVICE（默认 cuda）
        │
        ▼
目标设备 == cuda ？──否──► 不编译 TurboMind，ext_modules=[]
        │ 是                    extra_deps=[]
        ▼                       （ascend/rocm/... 走 PyTorch 后端）
 DISABLE_TURBOMIND 为真？──是──► 同上：跳过编译
        │ 否
        ▼
编译 TurboMind：
  - 用 cmake_build_extension 包装一个 CMakeExtension（name=_turbomind）
  - get_turbomind_deps() 探测 nvcc 版本，补 nvidia-* 依赖
        │
        ▼
调用 setup()：
  install_requires = runtime_<device>.txt + extra_deps
  extras_require   = {all, lite, serve}
  ext_modules      = [_turbomind] 或 []
  entry_points     = lmdeploy 命令行
```

用布尔逻辑表示「是否编译 TurboMind」:

\[
\text{BuildTM} \;=\; (\text{target\_device} = \text{cuda}) \;\land\; \neg\, \text{DISABLE\_TURBOMIND}
\]

#### 4.2.3 源码精读

**(1) 识别目标设备**——这是整个 `setup.py` 的「第一问」([setup.py:13-14](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L13-L14)):

```python
def get_target_device():
    return os.getenv('LMDEPLOY_TARGET_DEVICE', 'cuda')
```

只读一个环境变量,默认 `cuda`。这个返回值会在后面被用来选择 `runtime_<device>.txt` 和 `requirements_<device>.txt`。

**(2) 探测 CUDA 版本补依赖**——只有要编译 TurboMind 时才需要 NCCL 等 nvidia 包([setup.py:35-55](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L35-L55)):

```python
def get_turbomind_deps():
    if os.name == 'nt':
        return []
    CUDA_COMPILER = os.getenv('CUDACXX', os.getenv('CMAKE_CUDA_COMPILER', 'nvcc'))
    nvcc_output = subprocess.check_output([CUDA_COMPILER, '--version'], ...).decode()
    CUDAVER, = re.search(r'release\s+(\d+).', nvcc_output).groups()
    if int(CUDAVER) >= 13:
        return ['nvidia-nccl-cu{CUDAVER}', 'nvidia-cuda-runtime',
                'nvidia-cublas', 'nvidia-curand']
    else:
        return [f'nvidia-nccl-cu{CUDAVER}', f'nvidia-cuda-runtime-cu{CUDAVER}',
                f'nvidia-cublas-cu{CUDAVER}', f'nvidia-curand-cu{CUDAVER}']
```

它调用 `nvcc --version` 解析出大版本号,CUDA 13+ 用无后缀的包名,旧版本用带 `cu{版本}` 后缀的包名。`CUDACXX` 环境变量可指定非默认路径的 nvcc。

**(3) 解析 requirements 的 `-r` 引用**——`parse_requirements` 能递归处理 `-r other.txt`([setup.py:76-82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L76-L82)):

```python
def parse_line(line, current_fpath):
    if line.startswith('-r '):
        # Allow specifying requirements in other files
        target = line.split(' ')[1]
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current_fpath), target)
        for info in parse_require_file(target):
            yield info
    else:
        ...
```

这就是为什么根目录的 `requirements_cuda.txt` 写 `-r requirements/build.txt` 能生效——`parse_requirements` 看到 `-r` 就去读那个文件。注意它对相对路径做了基于当前文件目录的拼接,所以 `requirements/runtime_cuda.txt` 里的 `-r common.txt` 也能正确找到 `requirements/common.txt`。

**(4) 是否编译 TurboMind 的总开关**——本讲最关键的一段([setup.py:134-162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L134-L162)):

```python
if get_target_device() == 'cuda' and os.getenv('DISABLE_TURBOMIND', '').lower() not in ('yes', 'true', 'on', 't', '1'):
    import cmake_build_extension
    ext_modules = [
        cmake_build_extension.CMakeExtension(
            name='_turbomind',
            install_prefix='lmdeploy/lib',
            cmake_depends_on=['pybind11'],
            source_dir=str(Path(__file__).parent.absolute()),
            cmake_generator=None if os.name == 'nt' else 'Ninja',
            cmake_build_type=os.getenv('CMAKE_BUILD_TYPE', 'Release'),
            cmake_configure_options=[...],
        ),
    ]
    extra_deps = get_turbomind_deps()
    cmdclass = dict(build_ext=cmake_build_extension.BuildExtension, )
else:
    ext_modules = []
    cmdclass = {}
    extra_deps = []
```

读法:

- 条件为真 → 创建一个 `CMakeExtension`,名字 `_turbomind`,安装前缀 `lmdeploy/lib`(编译产物最终落在 `lmdeploy/lib/_turbomind...so`),构建类型默认 `Release`(可用 `CMAKE_BUILD_TYPE` 改成 `Debug`)。`cmake_build_extension` 是个第三方库(见 [build.txt:1](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/build.txt#L1)),它在 `build_ext` 阶段调用 CMake 去构建顶层 `CMakeLists.txt`。
- 条件为假(非 cuda 设备,或设置了 `DISABLE_TURBOMIND=1`)→ `ext_modules=[]`,**完全不编译 C++**,只装 Python 依赖,安装飞快。

`cmake_configure_options` 里还透传了若干 CMake 变量([setup.py:145-154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L145-L154)),其中 `BUILD_PY_FFI=ON`、`BUILD_MULTI_GPU=ON`(Windows 下 OFF)、`USE_NVTX=ON`(Windows 下 OFF)直接对应 `CMakeLists.txt` 里的同名 option。

**(5) 把一切交给 `setup()`**([setup.py:164-195](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L164-L195)),重点看依赖与入口的接线:

```python
setup(
    name='lmdeploy',
    version=get_version(),
    ...
    setup_requires=parse_requirements('requirements/build.txt'),
    tests_require=parse_requirements('requirements/test.txt'),
    install_requires=parse_requirements(f'requirements/runtime_{get_target_device()}.txt') + extra_deps,
    extras_require={
        'all': parse_requirements(f'requirements_{get_target_device()}.txt'),
        'lite': parse_requirements('requirements/lite.txt'),
        'serve': parse_requirements('requirements/serve.txt'),
    },
    ...
    entry_points={'console_scripts': ['lmdeploy = lmdeploy.cli:run']},
    ext_modules=ext_modules,
    cmdclass=cmdclass,
)
```

把这行映射关系记住,本模块就通了:

| setup() 字段 | 读取的 requirements | 含义 |
| --- | --- | --- |
| `install_requires` | `requirements/runtime_<device>.txt` + `extra_deps` | 默认装的运行时依赖 |
| `extras_require['all']` | 根目录 `requirements_<device>.txt` | `pip install lmdeploy[all]` 时全套 |
| `extras_require['lite']` | `requirements/lite.txt` | 量化子命令需要的依赖 |
| `extras_require['serve']` | `requirements/serve.txt` | 起服务需要的依赖 |
| `setup_requires` | `requirements/build.txt` | 编译期需要(cmake_build_extension/pybind11) |
| `entry_points` | — | 注册 `lmdeploy` 命令行,指向 [`lmdeploy/cli/__init__.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/__init__.py) 里 re-export 的 `run`(来自 `entrypoint.py`) |

> 常见误区:根目录的 `requirements_cuda.txt` 不是默认安装内容,而是 `[all]` extra 的内容。默认 `pip install lmdeploy` 只装 `runtime_cuda.txt` 那一份。想连 build+lite+serve 一起装,要 `pip install lmdeploy[all]`。

#### 4.2.4 代码实践

**实践目标**:不实际编译,只读源码,确认「关掉 TurboMind 编译后,setup.py 会走哪条分支」。

**操作步骤**:

1. 打开 [setup.py:134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L134),看清 `if` 条件的两个部分。
2. 假设环境变量 `DISABLE_TURBOMIND=1`、`LMDEPLOY_TARGET_DEVICE` 未设置(默认 cuda),手动求值:`'cuda' == 'cuda'` 为真;`'1'.lower() not in ('yes','true','on','t','1')` 为假;整体为假。
3. 跟着进入 `else` 分支([setup.py:159-162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L159-L162)),确认 `ext_modules=[]`、`extra_deps=[]`。

**需要观察的现象**:`ext_modules` 为空时,`setup()` 调用里没有任何 CMake 编译,pip 只会解析 Python 依赖。

**预期结果**:你能说清「`DISABLE_TURBOMIND=1 pip install -e .` 之所以快,是因为它跳过了 CMake 构建,只装 Python 依赖,得到的 lmdeploy 只能跑 PyTorch 后端,不能跑 TurboMind」。

#### 4.2.5 小练习与答案

**练习 1**:`get_turbomind_deps()` 在 `os.name == 'nt'`(Windows)时直接返回 `[]`。结合后面 CMake 选项里多处 `if os.name == 'nt'` 的判断,推测在 Windows 上 TurboMind 是怎样的支持状态。

> **参考答案**:Windows 上 `get_turbomind_deps` 返回空,说明不通过 pip 自动补 nvidia CUDA 运行时包(Windows 用户需自行装好 CUDA Toolkit);同时 CMake 选项里 `BUILD_MULTI_GPU`、`USE_NVTX`、`cmake_generator` 都对 Windows 做了特殊处理(用默认 generator、关掉多 GPU 和 NVTX)。综合看 TurboMind 在 Windows 上是「可编译但功能裁剪、需用户自备工具链」的状态。

**练习 2**:`entry_points={'console_scripts': ['lmdeploy = lmdeploy.cli:run']}` 这行解决了什么问题?

> **参考答案**:它把 Python 函数 `lmdeploy.cli:run` 注册成一个名为 `lmdeploy` 的 shell 命令。安装后用户在终端敲 `lmdeploy serve ...`、`lmdeploy lite ...` 时,实际就是调用这个 `run` 函数(它再分发到各子命令)。这正是 [u1-l5 CLI 工具体系](u1-l5-cli-toolchain.md) 要讲的入口。

---

### 4.3 CMakeLists.txt 与 TurboMind C++ 扩展构建

#### 4.3.1 概念说明

上一模块我们看到:当条件满足时,`setup.py` 会通过 `cmake_build_extension` 调用 CMake 去构建顶层 `CMakeLists.txt`。这个 `CMakeLists.txt` 是 TurboMind 引擎的 **C++/CUDA 构建总脚本**,它负责:

1. 声明项目、要求 CMake 与 CUDA 工具链版本;
2. 拉取并编译第三方依赖(cutlass、fmt、yaml-cpp、xgrammar 等,通过 `FetchContent`);
3. 根据检测到的 CUDA 版本,选定要为哪些 GPU 架构生成代码;
4. 把 `src/` 子目录的 TurboMind 源码纳入编译;
5. 产出 `_turbomind`(以及 `_xgrammar`)两个 Python 扩展模块,安装到 `lmdeploy/lib/`。

注意:`CMakeLists.txt` 本身是给 CMake 工具链读的,不是 Python。但 lmdeploy 用 `cmake_build_extension` 把它「嵌进」了 Python 的 `setup.py` 流程里,所以两者无缝衔接。

#### 4.3.2 核心流程

```text
cmake_build_extension 触发 → 配置 CMakeLists.txt
        │
        ▼
project(TurboMind LANGUAGES CXX CUDA)   ← 声明用 C++ 和 CUDA
find_package(CUDAToolkit REQUIRED)       ← 必须有 CUDA 工具链
        │
        ▼
FetchContent 拉第三方库：
  fmt(11.1.4) / cutlass(v3.9.2) / yaml-cpp / xgrammar(v0.1.27) / Catch2(仅测试)
        │
        ▼
读 option 开关：BUILD_MULTI_GPU / BUILD_PY_FFI / BUILD_TEST / USE_NVTX / BUILD_FAST_MATH
        │
        ▼
按 CUDA 版本设 CMAKE_CUDA_ARCHITECTURES（70/75/80/86/89/90a/120a/100a）
        │
        ▼
add_subdirectory(src)                    ← 编译 TurboMind 真正的 C++/CUDA 源码
        │
        ▼
install(TARGETS _turbomind _xgrammar → lmdeploy/lib)   ← 产物落到 Python 包里
```

#### 4.3.3 源码精读

**(1) 项目声明与 CUDA 工具链要求**([CMakeLists.txt:15-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L15-L18) 与 [CMakeLists.txt:33](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L33)):

```cmake
cmake_minimum_required(VERSION 3.25 FATAL_ERROR)
cmake_policy(SET CMP0074 NEW)
cmake_policy(SET CMP0104 OLD)
project(TurboMind LANGUAGES CXX CUDA)
...
find_package(CUDAToolkit REQUIRED)
```

`LANGUAGES CXX CUDA` 表示这个项目同时编译 C++ 和 CUDA 代码;`find_package(CUDAToolkit REQUIRED)` 强制要求本机装有 CUDA 工具链,否则配置阶段就报错。

**(2) 构建选项(option)**——这些开关可被 `setup.py` 的 `cmake_configure_options` 覆盖([CMakeLists.txt:41-45](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L41-L45)):

```cmake
option(BUILD_MULTI_GPU "Build multi-gpu support" ON)
option(BUILD_PY_FFI    "Build python ffi" ON)
option(BUILD_TEST      "Build tests" OFF)
option(SPARSITY_SUPPORT "Build project with Ampere sparsity feature support" OFF)
option(BUILD_FAST_MATH "Build in fast math mode" ON)
```

`BUILD_PY_FFI=ON` 控制 whether 编译 Python 绑定(即 `_turbomind` pybind 扩展);`BUILD_MULTI_GPU=ON` 控制多卡(NCCL)支持;`USE_NVTX`([CMakeLists.txt:199-203](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L199-L203))控制是否插入 NVIDIA 性能打点标记。

**(3) 拉取第三方库(FetchContent)**——CMake 会在配置阶段从 GitHub 下载并编译这些依赖。以 cutlass(NVIDIA 的高性能矩阵运算模板库)为例([CMakeLists.txt:76-89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L76-L89)):

```cmake
FetchContent_Declare(
  repo-cutlass
  GIT_REPOSITORY https://github.com/NVIDIA/cutlass.git
  GIT_TAG                 v3.9.2
  GIT_SHALLOW             ON
  ...
)
set(CUTLASS_ENABLE_HEADERS_ONLY ON CACHE BOOL "Enable only the header library")
FetchContent_MakeAvailable(repo-cutlass)
```

同样方式拉取的还有 `fmt`(11.1.4,格式化库,[CMakeLists.txt:49-57](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L49-L57))、`yaml-cpp`(读 config 用)、`xgrammar`(v0.1.27,结构化输出/grammar 约束)。这意味着 **从源码编译 TurboMind 需要能联网下载这些库**,离线环境要提前准备好。

**(4) 多卡支持与 NCCL 查找**——`BUILD_MULTI_GPU` 开启时,会去找系统的 NCCL 库([CMakeLists.txt:145-178](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L145-L178))。它甚至会用 `python -c "import nvidia.nccl"` 去 pip 装的 nvidia 包里定位 NCCL 路径:

```cmake
if(BUILD_MULTI_GPU)
    execute_process(
      COMMAND python -c "import importlib.util; print(importlib.util.find_spec('nvidia.nccl')...)"
      ...
    )
    ...
    find_package(NCCL)
    if (NCCL_FOUND)
        set(USE_NCCL ON)
        add_definitions("-DUSE_NCCL=1")
    endif ()
endif()
```

这解释了为什么 `get_turbomind_deps()` 要往依赖里加 `nvidia-nccl-cu*`:CMake 在构建期需要能 `import nvidia.nccl` 找到 NCCL 头文件和库。

**(5) 选定 GPU 算力架构**——这段是「编译时间为什么长」的主要原因之一,nvcc 要为每个架构生成一份 PTX/SASS([CMakeLists.txt:246-273](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L246-L273)):

```cmake
if(ARCH STREQUAL "x86_64")
  if (NOT CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES "")
    if (${CMAKE_CUDA_COMPILER_VERSION} VERSION_LESS "13.0")
      list(APPEND CMAKE_CUDA_ARCHITECTURES 70-real 75-real)  # V100, 2080
    endif()
    if (${CMAKE_CUDA_COMPILER_VERSION} VERSION_GREATER_EQUAL "11")
      list(APPEND CMAKE_CUDA_ARCHITECTURES 80-real) # A100
    endif ()
    ...
    if (${CMAKE_CUDA_COMPILER_VERSION} VERSION_GREATER_EQUAL "12.8")
      list(APPEND CMAKE_CUDA_ARCHITECTURES 120a-real) # 5090
    endif()
    if(${CMAKE_CUDA_COMPILER_VERSION} VERSION_GREATER_EQUAL "12.8")
      list(APPEND CMAKE_CUDA_ARCHITECTURES 100a-real) # B200
    endif()
  endif ()
```

每个 `list(APPEND ...)` 加一个架构:`70`(V100)、`75`(2080)、`80`(A100)、`86`(3090)、`89`(4090)、`90a`(H100)、`120a`(RTX 5090)、`100a`(B200)。nvcc 版本越新,支持的架构越多,要编译的目标也越多。这也解释了 README 里说的:从 **v0.13.0** 起,PyPI 预编译 wheel 基于 **CUDA 12.8** 构建,所以 RTX 50 系(架构 120a)开箱即用([README.md:219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L219))。

**(6) 编译真正的源码并安装产物**([CMakeLists.txt:374](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L374) 与 [CMakeLists.txt:381-389](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L381-L389)):

```cmake
add_subdirectory(src)          # TurboMind 的 C++/CUDA 源码在 src/ 下

if (BUILD_PY_FFI)
  if (CALL_FROM_SETUP_PY)
    install(TARGETS _turbomind DESTINATION ${CMAKE_INSTALL_PREFIX})
    install(TARGETS _xgrammar DESTINATION ${CMAKE_INSTALL_PREFIX})
  else()
    install(TARGETS _turbomind DESTINATION ${CMAKE_SOURCE_DIR}/lmdeploy/lib)
    install(TARGETS _xgrammar DESTINATION ${CMAKE_SOURCE_DIR}/lmdeploy/lib)
  endif()
endif ()
```

`add_subdirectory(src)` 把 TurboMind 的 C++ 源码(就是 [u1-l2](u1-l2-directory-and-architecture.md) 里说的 `src/` 目录)纳入编译;`CALL_FROM_SETUP_PY` 是 `setup.py` 透传过来的标记([setup.py:148](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L148)),决定产物装到哪——由 `setup.py` 驱动时装到 `install_prefix`(即 `lmdeploy/lib`),独立手动构建时装到源码树里的 `lmdeploy/lib`。最终 Python 端就能在 `lmdeploy/lib/` 下找到 `_turbomind...so`,进而 `from lmdeploy.lib import _turbomind`。

#### 4.3.4 代码实践

**实践目标**:把 `CMakeLists.txt` 与 `setup.py` 的对应关系串起来。

**操作步骤**:

1. 在 [setup.py:145-154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L145-L154) 的 `cmake_configure_options` 里找出三个透传给 CMake 的变量:`-DBUILD_PY_FFI=ON`、`-DBUILD_MULTI_GPU=...`、`-DUSE_NVTX=...`。
2. 回到 [CMakeLists.txt:41-45](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L41-L45) 和 [CMakeLists.txt:199-203](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L199-L203),确认这些变量正是 CMake 里的 `option(...)` 声明。
3. 统计 [CMakeLists.txt:246-273](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CMakeLists.txt#L246-L273) 里一共会为多少种 GPU 架构生成代码。

**需要观察的现象**:`setup.py` 用 `-D` 前缀把 Python 侧的决定「翻译」给 CMake,两套构建系统靠这些 `-D` 变量耦合。

**预期结果**:你能在一张图上画出 `setup.py (Python) → cmake_configure_options (-D...) → CMakeLists.txt (option/FetchContent/arch) → _turbomind.so` 这条完整链路。

#### 4.3.5 小练习与答案

**练习 1**:`CMakeLists.txt` 用 `FetchContent` 从 GitHub 下载 fmt、cutlass、yaml-cpp、xgrammar。这给「从源码编译」带来了什么隐含要求?

> **参考答案**:要求编译机器在配置阶段能访问 GitHub。在离线/内网环境里 `FetchContent` 会失败,需要预先 mirror 这些仓库或用本地路径替代。这也是为什么普通用户优先用预编译 wheel,而不是自己编译。

**练习 2**:`install(TARGETS _turbomind DESTINATION ...)` 里的目标名是 `_turbomind`,而 `setup.py` 里 `CMakeExtension(name='_turbomind')` 也叫这个名字,`install_prefix='lmdeploy/lib'`。这三者如何对应?

> **参考答案**:CMake 编译产出的扩展目标叫 `_turbomind`(对应一个 `_turbomind.cpython-xx-xxx.so` 文件);`CMakeExtension` 的 `name='_turbomind'` 告诉 setuptools 这个扩展模块的名字;`install_prefix='lmdeploy/lib'` 决定 `.so` 文件被装到 `lmdeploy/lib/` 目录。三者一致后,Python 才能通过 `lmdeploy.lib._turbomind` 这个路径 import 到 TurboMind 的 C++ 引擎。

---

## 5. 综合实践

本任务贯穿三个最小模块,目标:从「读依赖」到「亲手只装 PyTorch 后端」走一遍完整流程。

**任务**:用 `DISABLE_TURBOMIND=1 pip install -e .` 在本地以可编辑模式只安装 PyTorch 后端,并验证安装成功。

**操作步骤**:

1. **读依赖**。打开 `requirements_cuda.txt` 与 `requirements/runtime_cuda.txt`,挑出 3 个你认为最关键的运行时依赖并写一句话理由。参考答案(任选即可):
   - `torch`([runtime_cuda.txt:11](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt#L11)):PyTorch 后端的根基。
   - `triton`([runtime_cuda.txt:13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt#L13)):PyTorch 后端用 Triton 写自定义 kernel。
   - `accelerate`([runtime_cuda.txt:3](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/requirements/runtime_cuda.txt#L3)):加载 HF 模型时用。

2. **确认编译开关**。在 `setup.py` 第 134 行确认:`DISABLE_TURBOMIND=1` 时 `os.getenv('DISABLE_TURBOMIND', '').lower()` 得 `'1'`,它 **在** `('yes','true','on','t','1')` 里,所以条件为假,走 `else` 分支,不编译 C++。

3. **执行安装**(需要本机有 Python 3.10–3.13 与 pip):

   ```bash
   # 在仓库根目录
   export LMDEPLOY_TARGET_DEVICE=cuda   # 可省略，默认就是 cuda
   DISABLE_TURBOMIND=1 pip install -e .
   ```

   > 注意:此命令不编译 TurboMind,故 **不需要** nvcc/CMake 工具链,也不会触发 `FetchContent` 下载,安装速度接近「纯 Python 包」。

4. **验证安装**:

   ```bash
   python -c "import lmdeploy; print(lmdeploy.__version__)"
   lmdeploy --help
   ```

**需要观察的现象**:

- 第 3 步不会出现 `Building TurboMind` / `nvcc` / `cmake` 之类的编译输出,只有依赖解析与安装。
- 第 4 步能打印出版本号(当前源码版本为 `0.14.0`,见 [`lmdeploy/version.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/version.py)),`lmdeploy --help` 能列出 `serve`/`lite`/`chat` 等子命令。

**预期结果**:`import lmdeploy` 成功,证明只装 PyTorch 后端也能得到一个可用的 lmdeploy 包。此时若尝试用 TurboMind 后端(如 `TurbomindEngineConfig`)会因缺少 `_turbomind` 扩展而失败——这正是「轻装」的代价。

> **待本地验证**:第 3、4 步的实际耗时与输出取决于本机已装依赖与网络。若本机无 CUDA/无 GPU,这一步仍可成功(因为没编译 TurboMind),但跑真实推理时还需 GPU 与 torch 的 CUDA 版本。如无法在本机执行,请改为阅读型实践:只完成第 1、2 步,并口述 `DISABLE_TURBOMIND=1` 为何能让安装变快。

## 6. 本讲小结

- lmdeploy 是跨设备(cuda/ascend/rocm/maca/camb)项目,运行时依赖按设备拆成多套 `requirements`,安装时只加载目标设备那一套,避免装无用甚至冲突的包。
- 依赖分两层:根目录 `requirements_<device>.txt` 是 `[all]` 全套聚合,`requirements/runtime_<device>.txt` 是默认 `install_requires`;`common.txt` 被各 runtime 共享。
- `setup.py` 的核心是 `LMDEPLOY_TARGET_DEVICE`(选设备)与 `DISABLE_TURBOMIND`(是否编译 C++)两个开关;编译 TurboMind 的条件是 \(\text{BuildTM} = (\text{device}=\text{cuda}) \land \neg\,\text{DISABLE\_TURBOMIND}\)。
- TurboMind 由 `CMakeLists.txt` 构建:它拉取 cutlass/fmt/yaml-cpp/xgrammar 等第三方库,按 CUDA 版本选定 GPU 架构(70→100a),编译 `src/` 源码,产出 `_turbomind` 装到 `lmdeploy/lib/`。
- 普通用户走 `pip install lmdeploy`(用预编译 wheel,从 v0.13.0 起基于 CUDA 12.8);贡献者/特殊设备用户走 `setup.py` + CMake 从源码构建。
- `setup.py` 与 `CMakeLists.txt` 通过 `cmake_configure_options` 里的 `-D` 变量(`BUILD_PY_FFI`/`BUILD_MULTI_GPU`/`USE_NVTX`/`CALL_FROM_SETUP_PY`)耦合,Python 决策、CMake 执行。

## 7. 下一步学习建议

装好 lmdeploy 之后,下一步自然是 **真正跑一次推理**。建议进入:

- [u1-l4 pipeline 推理快速上手](u1-l4-pipeline-quickstart.md):用 `pipeline()` 跑通第一次文本推理与流式推理,把本讲装好的包真正用起来。
- [u1-l5 命令行工具体系 (CLI)](u1-l5-cli-toolchain.md):本讲提到的 `entry_points` 注册的 `lmdeploy` 命令,在那里系统讲解其子命令分发。

如果想深入 **构建链路本身**,可以继续读:

- [`lmdeploy/lib/`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lib) 目录:看 `_turbomind` 扩展被装进来后,Python 端如何 import 与桥接(详见 U6 TurboMind 后端)。
- [`src/`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src) 目录与 `src/CMakeLists.txt`:TurboMind 真正的 C++/CUDA 源码组织,承接本讲的 `add_subdirectory(src)`。
