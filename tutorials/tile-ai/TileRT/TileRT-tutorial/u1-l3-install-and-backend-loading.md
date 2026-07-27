# 环境安装与后端动态加载机制

## 1. 本讲目标

学完本讲，你应当能够：

- 按官方要求，用预编译 Docker 镜像或 wheel 把 TileRT 装到一个 **ABI 完全匹配** 的环境（CUDA 13.2 / torch 2.11.0+cu130 / Python 3.12 / B200）。
- 说清楚为什么 `import tilert` 时**不会**加载任何后端，而要等到 `tilert.load_backend(model_type)` 才真正把 `.so` 注入进程——也就是「懒加载」。
- 解释为什么一个 Python 进程**只能加载一个**后端，连续加载两个不同后端会抛 `RuntimeError`。
- 从原理上理解 `ctypes.CDLL(...)` 与 `torch.ops.load_library(...)` 这两步分别是干嘛，以及它们如何把 C++ 后端注册成 `torch.ops.tilert.*` 命名空间下的自定义算子。

本讲承接上一讲（[u1-l2 项目目录结构与双后端架构地图](u1-l2-directory-map.md)）建立的「双后端 `.so`、单进程单后端」认知，把那条认知从「结论」下沉到「源码层面到底怎么做到的」。

## 2. 前置知识

在进入源码前，先用大白话过几个本讲会反复出现的概念。

- **共享库（`.so`）**：Linux 上的动态链接库，相当于 Windows 的 `.dll`。一段编译好的 C++ 代码可以被打包进 `.so`，再在运行时被程序「加载」进来调用。TileRT 真正的运行时大脑就编译在 `libtilert_dsv32.so` / `libtilert_glm5.so` 里。
- **ctypes**：Python 标准库里用来调用 C/C++ 动态库的模块。`ctypes.CDLL("xxx.so")` 会把 `.so` 加载进当前进程的地址空间。
- **`RTLD_GLOBAL` / `RTLD_LAZY`**：加载 `.so` 时的两个标志位。`RTLD_GLOBAL` 表示「把这个库的符号放进**全局**符号表，后续加载的其他库也能看见」；`RTLD_LAZY` 表示「函数符号等到第一次被调用时才解析，不必一加载就全部绑定」。TileRT 用的是它们的按位或。
- **torch.ops 自定义算子注册**：PyTorch 提供的一套机制——C++ 端用 `TORCH_LIBRARY` 宏声明一个命名空间（比如 `tilert`）和若干算子，Python 端只要加载了对应的 `.so`，就能用 `torch.ops.tilert.xxx()` 直接调用这些算子。`torch.ops.load_library(path)` 就是触发这一注册的官方入口。
- **ABI（应用二进制接口）**：决定编译产物能否互相兼容的底层约定，包括函数调用栈布局、结构体内存排列等。版本对不上，运行时就会段错误或报「未定义符号」。这正是 TileRT 把依赖版本锁得死死的原因。
- **导入时加载 vs 懒加载**：前者指 `import` 包的瞬间就执行副作用（如加载 `.so`）；后者指把副作用推迟到真正需要时才执行。TileRT 选的是后者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md) | 给出官方安装步骤、版本锁定表、Docker 镜像与 wheel 地址、验证命令。 |
| [Dockerfile](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile) | 把「精确锁版本的依赖集合」固化成可复现镜像，是版本锁最权威的来源。 |
| [pyproject.toml](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml) | 声明 wheel 的运行期依赖与版本下限，并解释为何此处故意不写 `[build-system]`。 |
| [tilert/__init__.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py) | `load_backend` 的全部实现：懒加载、互斥校验、路径解析、`ctypes` + `torch.ops.load_library` 两步注册。 |
| [tilert/tilert_init.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/tilert_init.py) | 后端注册成功后，用 `torch.ops.tilert.tilert_init_op()` 做一次「握手」初始化。 |
| [tilert/generate.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) | CLI 入口里 `get_generator` 自动调用 `load_backend`，是「懒加载」的最佳证据。 |

---

## 4. 核心概念与源码讲解

### 4.1 Docker / wheel 安装与版本锁

#### 4.1.1 概念说明

TileRT 是一个**预编译二进制**项目：官方不让你在自己机器上现编 `.so`，而是直接发一个 wheel。这个 wheel 是针对**一组精确版本**编译出来的——CUDA 13.2、torch 2.11.0+cu130、Python 3.12、Blackwell 架构（sm_100，也就是 B200）。

这不是「建议」，而是**硬约束**。原因有二：

1. wheel 里的 `.so` 是用 torch 的 C++ 扩展 ABI 编出来的，必须和运行时 torch 的 ABI 严丝合缝。torch 不同小版本之间 ABI 可能不兼容，所以版本必须钉死。
2. torch 本身要用 PyTorch 官方的 **cu130 索引**（`https://download.pytorch.org/whl/cu130`）来装；如果直接从 PyPI 装 torch，拿到的是另一个 CUDA 分支的构建，链接不上 tilert 的二进制。

README 用一个表格把这套「构建环境」列了出来，并明确写道：**These are hard requirements, not lower bounds.**（这是硬性要求，不是版本下限。）

#### 4.1.2 核心流程

官方推荐的安装链路是：

1. **拉官方镜像**（最省心，版本已经全部锁好）。
2. **在容器里装 wheel**（从 PyPI 或 GitHub Release）。
3. **验证**：打印 `tilert.__version__` 与 `torch.version.cuda`，确认对得上。

伪代码描述：

```text
docker pull ghcr.io/tile-ai/tilert:cu132-latest
docker run --gpus all --ipc=host ...    # 进入容器
pip install tilert==0.1.5.post1         # 装预编译 wheel
python -c "import tilert, torch; ..."   # 验证版本号
```

如果你坚持不用镜像、要在宿主机装，就必须自己复现 Dockerfile 里那套锁版本依赖，否则运行时会出各种「未定义符号 / 找不到算子」的错误。

#### 4.1.3 源码精读

先看 README 的版本锁定表，它把「硬约束」讲得最直白：

[README.md:75-83](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L75-L83) —— 列出 GPU/驱动/OS/Python/torch/transformers/tokenizers 的精确版本，是判断「我的环境能不能跑」的清单。

[README.md:104-113](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L104-L113) —— `docker run` 进容器后，两种 wheel 安装方式：PyPI（`pip install tilert==0.1.5.post1`）或直接钉死 GitHub Release 的 wheel 直链。注意 cu130 索引在镜像里已经预配好，容器内 `pip install` 会自动用对。

[README.md:118-121](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L118-L121) —— 一行验证命令，期望输出 `cuda 13.0`（这是 `torch.version.cuda` 对 cu130 构建的回报值，与系统级 CUDA 13.2 驱动兼容）。

这套锁版本依赖，最权威、最完整的来源其实是 **Dockerfile**。它把每一个传递依赖都钉死了版本：

[Dockerfile:18](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile#L18) —— 基础镜像 `pytorch/manylinux2_28-builder:cuda13.2-main`，决定了 glibc ≥ 2.28（manylinux_2_28）和 CUDA 13.2 工具链。

[Dockerfile:33-35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile#L33-L35) —— `conda create -n tilert python=3.12.9`，把 Python 钉到 3.12.9。

[Dockerfile:44-57](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile#L44-L57) —— 关键的 cu130 索引设置（`PIP_INDEX_URL=https://download.pytorch.org/whl/cu130`），以及把 `torch==2.11.0+cu130` / `triton==3.6.0` / `transformers==4.46.3` / `tokenizers==0.20.3` 等逐个钉版本。这一段就是你脱离镜像时必须照抄的清单。

[Dockerfile:97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile#L97) —— 装完后立刻用 `assert` 自检：`torch.version.cuda.startswith("13")`、`transformers.__version__ == "4.46.3"` 等。镜像构建期就会挡掉版本漂移，是「版本锁」最硬的一道闸。

[Dockerfile:104-109](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile#L104-L109) —— `CUDAARCHS="100"` / `TORCH_CUDA_ARCH_LIST="10.0"`，把 GPU 架构钉到 Blackwell sm_100，即 B200。这也是为什么 TileRT 必须跑在 B200 上。

而 `pyproject.toml` 里只写「运行期依赖」，并且故意只写了一个面向 cu130 的纯版本号：

[pyproject.toml:17-28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml#L17-L28) —— `torch==2.11.0`、`transformers==4.46.3`、`tokenizers==0.20.3`。注意注释明确提醒：**torch 必须从 PyTorch 的 cu130 索引装**，从 PyPI 装会拿到不匹配的 CUDA 构建。这个注释是排错时最容易忽略、却最关键的一句。

[pyproject.toml:68-71](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml#L68-L71) —— 解释为何这个公开仓库**故意不写 `[build-system]`**：wheel 是在内部开发仓（`TileRT-dev/TileRT`）用 `scikit-build-core` 编出来的，这里只是发布用的「展示副本」，不希望有人误把本仓 `pip wheel .` 出来用一个错 ABI 的产物。

#### 4.1.4 代码实践

> **实践目标**：在容器内验证安装无误，并确认 CUDA 版本与期望一致。

操作步骤（在有 8× B200 的机器上）：

1. 拉镜像并进入容器：
   ```bash
   docker pull ghcr.io/tile-ai/tilert:cu132-latest
   docker run --rm -it --gpus all --ipc=host \
       -v "$PWD":/workspace -w /workspace \
       ghcr.io/tile-ai/tilert:cu132-latest
   ```
2. 容器内装 wheel：
   ```bash
   pip install tilert==0.1.5.post1
   ```
3. 跑 README 给的那行验证命令：
   ```bash
   python -c "import tilert, torch; print(tilert.__version__, torch.version.cuda)"
   ```

需要观察的现象 / 预期结果：

- `tilert.__version__` 打印出 `0.1.5.post1`（若你是 `pip install -e .` 装的开发副本且没设 tag，可能显示 `0.0.0` 或 `0.1.0` 兜底值——见下一节 4.3 讲的 `__version__` 兜底逻辑）。
- `torch.version.cuda` 打印出以 `13.` 开头的字符串（README 给的期望是 `13.0`）。
- 整个过程**不会**触发后端 `.so` 的加载——`import tilert` 只是导入纯 Python 包，不会去 `ctypes.CDLL`。你可以用 `python -c "import tilert; print(tilert._loaded_backend)"` 验证它此时是 `None`。

> 如果手头没有 B200 机器，本步骤属于**待本地验证**：你仍可在普通机器上 `import tilert` 并打印版本号，但只要一调 `load_backend`，后端 `.so` 会在初始化 B200 算子时报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能为了「省事」把 wheel 装到一台已有的、torch 版本是 2.10 的服务器上？

> **参考答案**：wheel 里的 `libtilert_*.so` 是按 torch 2.11.0+cu130 的 C++ 扩展 ABI 编出来的。torch 2.10 与 2.11 之间的 ABI 不保证兼容，运行时会出现「undefined symbol」之类的动态链接错误，或算子注册失败。README 把这些版本列为 hard requirements 而非下限，正是这个原因。

**练习 2**：Dockerfile 里 `PIP_INDEX_URL=https://download.pytorch.org/whl/cu130` 这一行，如果删掉会怎样？

> **参考答案**：`pip install torch==2.11.0+cu130` 会去默认源（PyPI）找，而 PyPI 上 `torch==2.11.0` 是另一个 CUDA 分支的构建（不带 `+cu130` 后缀的那个甚至根本不存在该 tag）。结果是装不到正确的 cu130 构建，链接不上 tilert 二进制。

---

### 4.2 `load_backend` 懒加载流程与单后端互斥

#### 4.2.1 概念说明

上一讲我们已经知道：TileRT 有两个后端 `.so`，一个进程只能装一个。本节把这个结论拆到代码层面。

`tilert/__init__.py` 的模块 docstring 一开篇就把设计意图讲透了：

[tilert/__init__.py:1-12](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L1-L12) —— 明确写着「它们**不在导入时加载**，由调用方通过 `load_backend(model_type)` 选择，且每进程只能装一个」。

为什么做成**懒加载**？因为：

- `import tilert` 可能在很多场景下发生（比如只是想读个版本号、跑个工具脚本），如果一 import 就去加载几十 MB 的 `.so`、还占用全局符号表，太重、太容易出副作用。
- 选哪个后端是业务决策（要跑 DeepSeek 还是 GLM-5），应该由调用方（CLI 或用户代码）显式指定，而不是包自动替你决定。

为什么做成**单后端互斥**？因为两个 `.so` 都把算子注册进**同一个** `torch.ops.tilert.*` 命名空间，并且用 `RTLD_GLOBAL` 全局加载（见 4.3）。第二个 `.so` 加载进来会和第一个的符号打架，行为不可预测。所以与其让你踩坑，不如直接拒绝。

#### 4.2.2 核心流程

`load_backend(model_type)` 的执行逻辑可以用下面这段伪代码概括：

```text
so_name = _BACKENDS[model_type]            # 查表，未知 → ValueError
if 已加载过某个后端:
    if 已加载的就是 so_name:
        return                              # 幂等：同一个后端重复加载是 no-op
    else:
        raise RuntimeError                   # 互斥：换了别的后端直接拒绝
lib_path = 包目录 / so_name
if lib_path 不存在:
    尝试兜底 libtilert.so；还没有 → RuntimeError
ctypes.CDLL(lib_path, RTLD_GLOBAL | RTLD_LAZY)   # 第一步：注入进程
torch.ops.load_library(lib_path)                  # 第二步：注册算子
_loaded_backend = so_name                         # 记下「当前后端」，供下次互斥判断
```

两个关键设计点：

- **幂等**：对**同一个**后端多次调用 `load_backend` 是安全的，第二次直接 `return`，不会重复加载。
- **互斥**：一旦加载了 A，再想加载 B 就抛 `RuntimeError`，并提示「另起一个进程」。

#### 4.2.3 源码精读

[tilert/__init__.py:43-48](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L43-L48) —— `_BACKENDS` 字典把两个合法 `model_type` 映射到各自的 `.so` 文件名；`_loaded_backend` 是进程级的「当前已加载后端」状态，初始为 `None`。这两个全局量是互斥机制的全部状态。

[tilert/__init__.py:51-68](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L51-L68) —— 函数入口与互斥校验。第 60-61 行：传入一个不认识的 `model_type`（比如拼错成 `"deepseek"`）会抛 `ValueError` 并列出所有合法值。第 62-68 行是核心互斥逻辑：

```python
if _loaded_backend is not None:
    if _loaded_backend != so_name:
        raise RuntimeError(
            f"TileRT backend '{_loaded_backend}' already loaded; cannot load "
            f"'{so_name}' in the same process. Run {model_type} in a fresh process."
        )
    return
```

注意两个分支：`!=` 时抛错（换后端），相等时直接 `return`（幂等）。

[tilert/__init__.py:69-78](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L69-L78) —— 路径解析与真正的加载。先在包目录下找 `libtilert_dsv32.so` / `libtilert_glm5.so`；找不到则兜底去找一个统一的 `libtilert.so`（兼容只有一个合并库的旧/开发构建）；都没有就抛错。找到后执行两步加载（详见 4.3），最后把 `so_name` 赋给 `_loaded_backend`。

而 CLI 端之所以不需要用户手动调 `load_backend`，是因为 `generate.py` 的 `get_generator` 自动替你调了：

[tilert/generate.py:36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L36) —— `tilert.load_backend(model_type)` 紧接着才是「按 `model_type` 延迟 import 对应 Generator」。这就是「懒加载」在真实调用链里的落点：先加载后端，再 import 模型代码。

#### 4.2.4 代码实践

> **实践目标**：亲眼看到「单后端互斥」的报错，并解释它为什么必须拒绝。

操作步骤（容器内、B200 机器上）：

1. 写一个脚本 `try_two_backends.py`：
   ```python
   # 示例代码
   import tilert

   tilert.load_backend("deepseek_v3_2")
   print("first load OK, _loaded_backend =", tilert._loaded_backend)

   tilert.load_backend("glm5")  # 预期：抛 RuntimeError
   ```
2. 运行 `python try_two_backends.py`。

需要观察的现象：

- 第一行打印 `first load OK, _loaded_backend = libtilert_dsv32.so`。
- 第二次调用直接抛 `RuntimeError`，错误信息形如：`TileRT backend 'libtilert_dsv32.so' already loaded; cannot load 'libtilert_glm5.so' in the same process. Run glm5 in a fresh process.`

预期结果与解释：两个后端都用 `RTLD_GLOBAL` 注册同一个 `tilert` 算子命名空间，符号会冲突；与其让运行时出现「算子被错误的后端实现覆盖」的隐蔽 bug，TileRT 选择在加载阶段就硬性拒绝。解决办法就是错误信息里说的——**另起一个进程**跑另一个模型。

附加实验（幂等性验证）：把第二步换成**再次** `tilert.load_backend("deepseek_v3_2")`，预期不报错、也不重复加载，函数直接返回（可在 `load_backend` 末尾的 `logger.info` 处加日志观察它没有被第二次触发）。

> 在没有 B200 的机器上，第一次 `load_backend` 就可能在算子初始化阶段失败，此实验属于**待本地验证**。但互斥校验本身（`_loaded_backend != so_name`）发生在 `ctypes.CDLL` **之后**才赋值，所以你至少能在赋值前的报错里看到加载失败信息；幂等/互斥的代码逻辑可在阅读层面理解。

#### 4.2.5 小练习与答案

**练习 1**：如果用户先 `load_backend("glm5")`，再 `load_backend("glm5")`，会发生什么？

> **参考答案**：第二次调用命中 4.2.3 中第 62-68 行的「相等则 `return`」分支，直接返回，**不**重复 `ctypes.CDLL`，也**不**报错。这就是幂等性，方便上层代码无脑调用而不用担心重复加载。

**练习 2**：错误信息为什么建议「另起一个进程」而不是「先卸载旧后端再加载新的」？

> **参考答案**：因为 `.so` 是用 `RTLD_GLOBAL` 加载进进程全局符号表的，C++ 侧注册进 `torch.ops.tilert.*` 的算子也没有提供「反注册」接口。一旦加载就不可撤销。最干净、最可靠的办法就是开一个新进程，让 OS 把地址空间和符号表整个清掉重来。

---

### 4.3 `ctypes` + `torch.ops.load_library` 算子注册

#### 4.3.1 概念说明

后端 `.so` 里是 C++ 写的 tile 级运行时。Python 怎么调用到这些 C++ 函数？答案分两步，缺一不可：

1. **`ctypes.CDLL(lib_path, mode=RTLD_GLOBAL | RTLD_LAZY)`**：把这个 `.so` 映射进当前进程的地址空间，并把它的符号暴露到**全局**符号表。这一步是「物理加载」——让进程能看见这个库。
2. **`torch.ops.load_library(lib_path)`**：PyTorch 官方提供的 API，它会去执行 `.so` 里用 `TORCH_LIBRARY` 宏声明的注册代码，把算子挂到 `torch.ops.tilert.*` 命名空间下。这一步是「逻辑注册」——让 `torch.ops.tilert.xxx()` 能被 Python 调用。

为什么两步都要？因为 `torch.ops.load_library` 只负责触发 `TORCH_LIBRARY` 注册，而 `.so` 内部可能还依赖一些**全局可见**的符号（比如两个库都要用到的 CUDA / NCCL 句柄，或者 tilert 自己的全局单例）。`RTLD_GLOBAL` 保证这些符号对后续所有库都可见，避免「未定义符号」错误。`RTLD_LAZY` 则是性能优化：函数符号等到真正调用时才解析，加速启动、也允许 `.so` 带一些「暂时用不到所以不报错」的弱符号。

两步完成之后，C++ 后端里的算子就变成了 Python 里可以直接调的 `torch.ops.tilert.<算子名>(...)`。`tilert_init` 就是这样一个算子。

#### 4.3.2 核心流程

```text
load_backend(model_type):
    ... (互斥校验、路径解析见 4.2) ...
    ctypes.CDLL(lib_path, mode=RTLD_GLOBAL | os.RTLD_LAZY)   # ① 物理加载到全局符号表
    torch.ops.load_library(lib_path)                          # ② 触发 TORCH_LIBRARY 注册
    _loaded_backend = so_name
# ── 之后任意代码即可调用 ──
tilert_init()                                                  # ③ 调用注册好的算子做一次握手
# 它内部等价于: torch.ops.tilert.tilert_init_op()
```

注意第 ③ 步：注册只是让算子「可调」，真正初始化后端运行时（分配全局状态、设置 GPU 设备等）是靠显式调用 `tilert_init_op` 这个算子完成的——这是 Python 与 C++ 后端的「握手点」。

#### 4.3.3 源码精读

[tilert/__init__.py:76-78](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L76-L78) —— 两步加载的核心两行：

```python
ctypes.CDLL(str(lib_path), mode=ctypes.RTLD_GLOBAL | os.RTLD_LAZY)
torch.ops.load_library(str(lib_path))
_loaded_backend = so_name
```

`mode=ctypes.RTLD_GLOBAL | os.RTLD_LAZY` 是关键：`RTLD_GLOBAL` 让符号全局可见（两个后端都依赖这一点，但也正因如此它们会撞车，见 4.2），`RTLD_LAZY` 延迟符号解析。

[tilert/__init__.py:23-29](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L23-L29) —— 一个容易被忽略的小细节：导入时校验 `torch.ops` 存在；`__version__` 用 `importlib.metadata.version("tilert")` 读，读不到（比如未通过 pip 安装）就兜底成 `"0.0.0"`。这正是 4.1.4 实践里「`-e` 安装可能版本号不对」的根因。

[tilert/__init__.py:84-91](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L84-L91) —— `tilert_init` 在 `__init__.py` 末尾被 `import`，但**它本身不在 import 时执行**——它只是个普通函数，定义里调用 `torch.ops.tilert.tilert_init_op()`，只有显式调用才生效。

[tilert/tilert_init.py:11-18](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/tilert_init.py#L11-L18) —— 这就是握手算子的全部包装。`tilert_init()` → `torch.ops.tilert.tilert_init_op()`。注意 `torch.ops.tilert.tilert_init_op` 这个名字是在 C++ 端 `TORCH_LIBRARY("tilert", ...)` + `def("tilert_init_op", ...)` 里定下来的；它必须等 `load_backend` 调用了 `torch.ops.load_library` 之后才存在。

[tilert/models/deepseek_v3_2/generator.py:91-93](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L91-L93) —— 真实调用链里的握手点：Generator 的 `init()` 里调用 `tilert_init()`。也就是说，`load_backend`（注册算子）发生在 `get_generator` 开头，`tilert_init`（执行握手算子）发生在稍后 `generator.init()` 时——两者都在 `from_pretrained` 加载权重之前。

#### 4.3.4 代码实践

> **实践目标**：观察「加载后端前后，`torch.ops.tilert` 命名空间的变化」，直观看到 `torch.ops.load_library` 的注册效果。

操作步骤（容器内、B200 机器上）：

1. 写脚本 `observe_registration.py`：
   ```python
   # 示例代码
   import torch
   import tilert  # 仅 import，尚未 load_backend

   def list_tilert_ops():
       try:
           ns = torch.ops.tilert
           # 列出命名空间下已注册的算子名
           return sorted(n for n in dir(ns) if not n.startswith("_"))
       except (AttributeError, RuntimeError):
           return []

   print("before load_backend:", list_tilert_ops())
   tilert.load_backend("deepseek_v3_2")
   print("after  load_backend:", list_tilert_ops())
   ```
2. 运行 `python observe_registration.py`。

需要观察的现象：

- `before load_backend` 多半是**空列表**（或干脆报 `tilert` 命名空间不存在）——印证了「`import tilert` 不加载后端」。
- `after load_backend` 应当能看到一批算子名，其中至少包含 `tilert_init_op`。

预期结果：调用 `load_backend` 之后，C++ 后端里所有用 `TORCH_LIBRARY("tilert", ...)` 注册的算子都出现在 `torch.ops.tilert.*` 下。这时你再调 `tilert.tilert_init()` 就不会报「找不到算子」了。

附加小实验：在 `load_backend` 之前故意调用 `torch.ops.tilert.tilert_init_op()`，预期抛 `torch` 的「operator not found / namespace not registered」类错误——这是注册尚未发生的铁证。

> 在无 B200 的机器上，`load_backend` 内部的 `ctypes.CDLL` 仍可能成功（只是把库映射进来），但 `tilert_init_op()` 真正跑起来会因找不到 sm_100 设备而失败，本实验属于**待本地验证**。算子是否出现在命名空间里这一点，是可以在加载阶段（不真正执行算子）观察到的。

#### 4.3.5 小练习与答案

**练习 1**：为什么注册要分 `ctypes.CDLL` 和 `torch.ops.load_library` 两步，而不是只用后者？

> **参考答案**：`torch.ops.load_library` 只负责触发 `.so` 内部的 `TORCH_LIBRARY` 注册代码。而该 `.so` 内部可能引用了一些需要被「全局可见」的符号（如 CUDA 运行时句柄、tilert 自己的全局单例）。`ctypes.CDLL(..., RTLD_GLOBAL)` 先把这些符号以全局方式暴露出来，避免 `.so` 在执行注册代码时因「未定义符号」而加载失败。两步各有分工，缺一不可。

**练习 2**：`tilert_init()` 为什么不能放在 `import tilert` 时自动执行？

> **参考答案**：`tilert_init()` 调用的是 `torch.ops.tilert.tilert_init_op()`，这个算子只有在 `load_backend` 执行了 `torch.ops.load_library` 之后才存在。而 `import tilert` 时尚未发生 `load_backend`（懒加载），此时算子还不存在，贸然调用会报「operator not found」。所以握手必须排在 `load_backend` 之后，按「加载 `.so` → 注册算子 → 调用 `tilert_init_op`」的顺序进行。

---

## 5. 综合实践

把本讲三块内容（版本锁 / 懒加载与互斥 / 算子注册）串起来，完成下面这个端到端的小任务。

**任务**：在官方镜像里，亲手验证「TileRT 的后端加载是一个有状态的、一次性的、按需触发的过程」，并把每一步的现象记录下来。

建议步骤：

1. **装环境并验证版本**：按 4.1.4 拉镜像、装 wheel、打印 `tilert.__version__` 与 `torch.version.cuda`。
2. **证明「import 不加载后端」**：在新进程里只执行 `python -c "import tilert; print(tilert._loaded_backend)"`，预期打印 `None`。
3. **观察算子注册变化**：跑 4.3.4 的 `observe_registration.py`，对比 `load_backend` 前后 `torch.ops.tilert` 命名空间的内容。
4. **触发互斥**：跑 4.2.4 的 `try_two_backends.py`，记录 `RuntimeError` 的完整错误信息，并用自己的话写一段解释：为什么错误信息建议「另起进程」而不是「卸载重装」。
5. **画出时序**：把上述过程画成一张时序图，至少包含这五个节点——`import tilert` → `load_backend("deepseek_v3_2")`（内部又分 `ctypes.CDLL` 与 `torch.ops.load_library` 两步）→ `generator.init()` 里的 `tilert_init()` → `from_pretrained` 加载权重 → `generate` 解码。标注每一步「`.so` 是否已加载」「算子是否已注册」「握手算子是否已执行」三个状态位的取值。

**交付物**：一份时序图 + 一份三列状态表（步骤 / `.so` 是否加载 / 算子是否注册 / 握手是否完成）。这张图将是你进入下一讲（[u1-l4 CLI 入口与生成流程](u1-l4-cli-entry-and-generation-flow.md)）前最重要的认知脚手架——因为它解释了 CLI 的第一条语句为什么是 `load_backend`，而不是 `import` 时就万事俱备。

> 无 B200 环境时，第 1-2 步和「符号/算子是否出现」的观察可在普通机器上完成；涉及真正执行算子的步骤属于**待本地验证**。

## 6. 本讲小结

- TileRT 以**预编译 wheel** 形态发布，依赖版本（CUDA 13.2 / torch 2.11.0+cu130 / Python 3.12 / transformers 4.46.3 / B200 sm_100）是**硬约束**，最完整的锁版本清单在 [Dockerfile](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/Dockerfile)，官方推荐用镜像复现环境。
- `import tilert` **不会**加载后端 `.so`；后端是**懒加载**的，由 `tilert.load_backend(model_type)` 显式触发，CLI 里这一步发生在 [generate.py:36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L36) 的 `get_generator` 开头。
- 一个进程**只能加载一个**后端：同后端重复调用**幂等**，换不同后端直接抛 `RuntimeError`，因为两个 `.so` 都以 `RTLD_GLOBAL` 注册同一个 `torch.ops.tilert.*` 命名空间会撞车。
- 后端注册分两步：`ctypes.CDLL(lib, RTLD_GLOBAL | RTLD_LAZY)` 把库物理加载进全局符号表，`torch.ops.load_library(lib)` 触发 C++ 端 `TORCH_LIBRARY` 把算子挂到 `torch.ops.tilert.*` 下。
- 注册完成后，还需要 `tilert_init()`（等价于调用算子 `torch.ops.tilert.tilert_init_op()`）做一次握手，真正初始化后端运行时——它在 Generator 的 `init()` 里被调用。
- 验证安装最简单的一行命令是 `python -c "import tilert, torch; print(tilert.__version__, torch.version.cuda)"`；验证互斥只需在一个进程里连续 `load_backend` 两个不同 `model_type`。

## 7. 下一步学习建议

到此你已经把「后端 `.so` 是怎么被加载和注册的」彻底搞懂。下一讲 [u1-l4 CLI 入口 tilert.generate 与生成流程](u1-l4-cli-entry-and-generation-flow.md) 会向上走一层，看 CLI 是怎么用 `argparse` 定义参数、怎么从 `~/.tilert/config.toml` 解析权重路径、又怎么通过 `get_generator` 把 `load_backend` 和模型组装串起来的。

建议你在此之前：

- 自己跑一遍本讲综合实践的时序图，确保能不假思索地说出「import → load_backend → tilert_init → from_pretrained → generate」这条链。
- 翻一下 [tilert/generate.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py) 的 `parse_args` 和 `get_weights_dir`，带着「为什么 CLI 必须显式 `--model`」的问题进入下一讲。
