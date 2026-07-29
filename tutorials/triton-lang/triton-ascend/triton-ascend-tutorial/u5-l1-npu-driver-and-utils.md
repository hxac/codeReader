# NPUDriver 与 NPUUtils：设备发现与架构探测

## 1. 本讲目标

本讲进入 Triton-Ascend 的**运行时侧**。学完后你应该能够：

- 说出 `NPUDriver` 作为 Ascend 后端「运行时门面」的职责，以及它与编译侧 `AscendBackend` 的分工。
- 解释 `NPUDriver.is_active()` 是如何判断「这台机器现在能不能用 Ascend」的，以及它检测的其实是**编译器**而非**硬件**这一关键事实。
- 理解 `NPUUtils` 如何把一段 C++ 源码 `npu_utils.cpp` 现场编译成 `npu_utils.so`、缓存、再动态加载来探测硬件。
- 读懂 `npu_utils.cpp` 这个 CPython 扩展模块如何通过 CANN 的 `rt*` 运行时 API 获取 arch、核数，并注册内核二进制。
- 认识 `NPU_DEVICE_LIMIT` 环境变量对核数的裁剪机制，以及 Vector 核数 = AI 核数 × 2 的由来。

本讲只讲「设备发现与探测」，即编译完成后、真正 `launch` 之前的「摸清家底」环节；内核启动（`rtKernelLaunch`、workspace、launcher 代码生成）留给 u5-l2 与 u5-l3。

## 2. 前置知识

在开始前，请确认你已建立以下概念（相关讲义见「下一步学习建议」之前的章节）：

- **CANN / BiSheng 工具链**（u1-l3）：CANN 是华为昇腾 NPU 的软件栈，编译期提供 BiSheng 编译器（`ccec`/`bishengir-compile`），运行期提供 ACL/runtime（如本讲会反复出现的 `rtGetSocVersion`、`rtGetAiCoreCount`、`rtKernelLaunch`）。装完 CANN 必须先 `source set_env.sh` 导出 `ASCEND_HOME_PATH` 等环境变量。
- **AscendBackend / NPUOptions**（u3-l2）：编译后端门面，管「把 TTIR 编译成 `.o`」。
- **NPU 硬件模型**（u2-l2）：一颗 AI 核内含 Cube Core（矩阵）与 Vector Core（向量）两类计算单元；软件层把 **Vector 核数定义为 AI 核数 × 2**。
- **entry points 后端发现机制**（u1-l2）：core 通过 `triton.backends` entry points 发现 `ascend` 后端，把 `third_party/ascend/backend` 挂载成 `triton.backends.ascend`。

本讲会用到几个 Python/C 概念，先一句话解释：

- **DriverBase**：core 定义的「驱动抽象基类」，规定每个后端必须实现 `is_active`、`get_current_target`、`get_current_device` 等方法（见 [python/triton/backends/driver.py:11-47](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/driver.py#L11-L47)）。`NPUDriver` 就是它的 Ascend 实现。
- **单例（singleton）**：一个类全局只允许存在一个实例。`NPUUtils` 用它避免反复编译 `npu_utils.so`。
- **`lru_cache`**：把函数返回值缓存，重复调用直接返回旧结果。用于昂贵的运行时探测。
- **CPython 扩展模块**：用 C/C++ 写、编译成 `.so`、可被 Python `import` 的模块。每个对 Python 可见的函数都用 `PyArg_ParseTuple`（解析入参）取参数、用 `Py_BuildValue`（组装返回值）回传结果。
- **`rtStream_t`**：CANN 的「流」句柄，类比 CUDA stream，内核异步提交到流上执行。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [third_party/ascend/backend/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py) | **主角**。定义 `NPUUtils`（硬件探测器）、`NPUDriver`（运行时门面）、`NPULauncher`（本讲只略提，u5-l2 详讲）以及 `NPU_DEVICE_LIMIT` 裁剪逻辑。 |
| [third_party/ascend/backend/npu_utils.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp) | CPython 扩展源码，被编译成 `npu_utils.so`。是 Python 与 CANN runtime 之间唯一的桥梁。 |
| [third_party/ascend/backend/utils.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py) | 辅助：`_build_npu_ext`（编译扩展）、`get_backend_func`（torch_npu/mindspore 分派）、`get_ascend_arch_from_env`（读 `TRITON_ASCEND_ARCH`）。 |
| [third_party/ascend/backend/backend_register.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py) | 把「当前设备」「当前流」等运行时接口按 torch_npu / mindspore 两套框架分别注册。 |
| [python/triton/runtime/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/driver.py) | core 侧：遍历所有后端、用 `is_active()` 选出唯一激活驱动。 |

## 4. 核心概念与源码讲解

### 4.1 NPUDriver：运行时门面与设备激活判断

#### 4.1.1 概念说明

`NPUDriver` 是 `DriverBase` 的 Ascend 实现，是「**编译完之后，把 kernel 跑起来**」所需的一切运行时能力的门面。它负责回答四类问题：

1. **激活判断**：这台机器现在能不能用 Ascend？(`is_active`)
2. **目标信息**：当前 target 是什么？（`get_current_target` → `backend="npu"` + `arch`）
3. **设备与流**：当前是哪张 NPU、当前流是什么？（`get_current_device`、`get_current_stream`）
4. **工具钩子**：类型映射、benchmarker、设备接口等（`map_python_to_cpp_type`、`get_benchmarker`、`get_device_interface`）

注意它和编译侧 `AscendBackend` 的**分工**：

| | 管什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `AscendBackend`（u3-l2） | 编译 | TTIR | `triton_xxx_kernel.o`（`.npubin`） |
| `NPUDriver`（本讲） | 运行时 | 已编译的 `.o` | 在指定 NPU/流上启动内核（u5-l3） |

一个管「怎么编」，一个管「在哪跑」。`NPUDriver` 本身不直接 `launch` 内核，而是把设备、流、target 这些「环境信息」提供给 launcher（`NPULauncher`，u5-l2）。

#### 4.1.2 核心流程

core 首次访问 `triton.runtime.driver.active` 时的选择流程（伪代码）：

```text
对每个已发现的后端 b（ascend / nvidia / amd ...）:
    if b.driver.is_active():               # 关键：调 is_active
        active_drivers.append(b.driver)
要求 len(active_drivers) == 1               # 恰好一个，否则报错
driver.active = active_drivers[0]()         # 实例化该 driver
```

之后所有编译/启动都从 `triton.runtime.driver.active` 取 target / device / stream。如果 `is_active` 没有恰好返回一个真值，整个 Triton 会在启动阶段就报错。

#### 4.1.3 源码精读

core 的选择逻辑位于 [python/triton/runtime/driver.py:6-10](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/driver.py#L6-L10)，用列表推导 `[x.driver for x in backends.values() if x.driver.is_active()]` 筛出激活驱动，并要求恰好 1 个。`is_active` 本身是 `DriverBase` 的抽象方法，见 [python/triton/backends/driver.py:13-16](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/driver.py#L13-L16)。

`NPUDriver` 类定义在 [third_party/ascend/backend/driver.py:199](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L199)，它的构造函数持有 `NPUUtils` 单例与 launcher 类：

```python
def __init__(self):
    self.utils = NPUUtils()
    self.launcher_cls = NPULauncher
    super().__init__()
```
（[driver.py:201-204](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L201-L204)）

`is_active` 是本模块的「总开关」。它内部定义并调用 `test_npucompiler()`：

```python
@classmethod
def is_active(cls):
    def test_npucompiler():
        from triton.backends.ascend.utils import _get_bisheng_path
        npucompiler = _get_bisheng_path()                       # 找到 bisheng(=ccec) 编译器
        targets = subprocess.check_output(
            [npucompiler, "-print-targets"]).decode().strip().split()
        return "hiipu64" in targets                             # 能编 hiipu64 才算 Ascend 就绪
    try:
        return test_npucompiler()
    except Exception as e_npucompiler:
        ...warnings.warn(红字错误)...
        return False
```
（[driver.py:206-222](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L206-L222)）

> **关键洞察**：`is_active` 检测的是「**BiSheng 编译器是否就位**」（能否编出 `hiipu64` 目标），**而不是**「NPU 硬件是否在位」。这意味着：一台只装了 CANN 工具链、没有实体 NPU 的机器，`is_active` 仍可能为 `True`——这正是 **compile-only / 纯编译**场景（如 costmodel、离线编译）能工作的原因。而真正「碰硬件」的 `get_current_device` / `get_arch` / `get_aicore_num` 才需要实体设备。

`get_current_target` 拼出 target 三元组：

```python
def get_current_target(self):
    backend = "npu"
    env_target = get_ascend_arch_from_env()        # 读 TRITON_ASCEND_ARCH
    if env_target:
        arch = env_target
    else:
        arch = self.utils.get_arch()               # 否则运行时探测
    warp_size = 0
    return GPUTarget(backend, arch, warp_size)
```
（[driver.py:227-235](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L227-L235)）

这里 arch 优先取环境变量 `TRITON_ASCEND_ARCH`（校验过的型号列表见 [utils.py:526-557](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L526-L557)），未设才运行时探测。注意 `GPUTarget` 名字带 "GPU" 是 Triton core 的历史命名，NPU 复用了它，并把 `warp_size` 填 `0`（NPU 没有 warp 概念）。`get_current_device` 则委托给 `get_backend_func("get_current_device")`，在 torch_npu 下就是 `torch.npu.current_device()`（[driver.py:237-241](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L237-L241) → [backend_register.py:241-245](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L241-L245)）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理解 core 是怎么在启动时挑出 `NPUDriver` 的。

1. 打开 [python/triton/runtime/driver.py:6-10](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/driver.py#L6-L10)，确认它对每个后端调 `is_active` 并要求恰好一个为真。
2. 追踪 `backends` 字典从哪来：读 [python/triton/backends/__init__.py:57-63](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/__init__.py#L57-L63)，确认它是通过 entry points 导入每个后端的 `compiler` 与 `driver` 模块。
3. **观察现象**：如果你给 `is_active` 的返回加一行日志（仅用于阅读，勿提交），首次 `import triton` 时会看到每个后端都被检测了一次。
4. **预期结果**：在装好 CANN 的 NPU 机器上，`ascend` 后端的 `is_active` 返回 `True`，其余为 `False`，最终 `driver.active` 是 `NPUDriver` 实例。

> 本实践为源码阅读型，无需实体设备即可完成阅读；若想观察运行期日志，需在 NPU 环境执行，「待本地验证」实际打印内容。

#### 4.1.5 小练习与答案

**练习 1**：如果 `is_active` 返回 `False`，core 会怎样？
**答案**：该驱动不会进入 `active_drivers`。若最终没有驱动为真，core 报 `0 active drivers`；若有多个为真，报数量错误。总之必须恰好一个。

**练习 2**：为什么 `get_current_target` 里 `warp_size = 0`？
**答案**：`warp`（线程束）是 GPU SIMT 模型的概念，昇腾 NPU 没有。Triton 的 `num_warps` 在 NPU 上映射为别的并发参数（见 u2-l2 的核数分配），所以这里填 `0` 占位。

---

### 4.2 NPUUtils：单例硬件探测器与 npu_utils.so 的编译

#### 4.2.1 概念说明

`NPUDriver` 只是个门面，真正「动手摸硬件」的是 `NPUUtils`。它的策略很特别：**把一段随包发布的 C++ 源码 `npu_utils.cpp`，在首次使用时现场编译成 `npu_utils.so`，缓存起来，再动态加载**，通过这个 `.so` 去调用 CANN runtime API。

为什么不在打包时就预编译好 `.so`？因为 `.so` 必须与用户机器上的 **Python 版本、CPython ABI、CANN 版本、torch_npu 版本**严格匹配，预编译二进制无法覆盖所有组合。Triton-Ascend 选择「源码随包 + 首次运行编译 + 内容寻址缓存」的折中：源码体积小、可移植，缓存让第二次起零成本。

`NPUUtils` 还是**单例**：通过重写 `__new__` 保证全局只有一个实例，避免每次 `new` 都触发一次编译。

#### 4.2.2 核心流程

`NPUUtils.__init__` 的编译-加载流程（伪代码）：

```text
1. 读 npu_utils.cpp 源码文本
2. key = md5(源码文本 + version_info)        # version_info = torch/torch_npu 的 git 版本
3. cache = get_cache_manager(key)
4. if 缓存里没有 npu_utils.so:
       在临时目录写源码 → _build_npu_ext 编译 → put 进缓存
5. importlib 动态加载该 .so → self.npu_utils_mod
```

之后 `get_arch()`、`get_aicore_num()` 这些方法，都是把调用转发给 `self.npu_utils_mod`（即那个 `.so`）。

#### 4.2.3 源码精读

单例实现——重写 `__new__`，用类属性 `instance` 缓存唯一实例（[driver.py:42-47](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L42-L47)）：

```python
class NPUUtils(object):
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(NPUUtils, cls).__new__(cls)
        return cls.instance
```

`__init__` 完成「读源码 → 哈希 → 查/写缓存 → 动态加载」全链路（[driver.py:49-70](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L49-L70)），关键片段：

```python
src = Path(src_path).read_text()                       # 读 npu_utils.cpp
version_info = get_backend_func("version_hash")        # torch/torch_npu git 版本
key = hashlib.md5((src + "_".join(version_info)).encode()).hexdigest()
cache = get_cache_manager(key)
fname = "npu_utils.so"
cache_path = cache.get_file(fname)
if cache_path is None or not os.path.exists(cache_path):
    ...tempdir 中 _build_npu_ext("npu_utils", tmp_src_path) 编译...
    cache_path = cache.put(f.read(), fname, binary=True)
spec = importlib.util.spec_from_file_location("npu_utils", cache_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
self.npu_utils_mod = mod
```

缓存键纳入 `version_info` 是**故意**的：torch_npu 版本一变，ABI 可能不兼容，必须重新编译 `.so`。`_build_npu_ext` 的实现在 [utils.py:389-445](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L389-L445)，它拼出 `clang++/g++` 命令，带上 CANN 的 include/lib 路径（`-lruntime -lascendcl`）和 torch_npu 的头文件。

探测方法都很薄，本质是转发：

| Python 方法 | 转发目标 | 行号 |
| --- | --- | --- |
| `get_arch()` | `npu_utils_mod.get_arch()` | [driver.py:138-140](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L138-L140) |
| `get_device_aicore()` | `npu_utils_mod.get_aicore_num()` | [driver.py:125-127](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L125-L127) |
| `get_aicore_num()` | `get_device_properties(...)["num_aicore"]` | [driver.py:142-144](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L142-L144) |
| `get_aivector_core_num()` | `get_device_properties(...)["num_vectorcore"]` | [driver.py:146-147](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L146-L147) |

注意 `get_device_aicore` 带 `@functools.lru_cache()`（[driver.py:125](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L125)），因为 `rtGetAiCoreCount` 是真正的运行时调用，结果在进程内不变，缓存掉避免每次问硬件。

> **易错点**：`get_aicore_num()` 与 `get_device_aicore()` **不是一回事**。后者（`get_device_aicore`）返回**硬件真实** AI 核数（直接转发 CANN API，且 lru_cache）；前者（`get_aicore_num`）经 `get_device_properties` → `_get_npu_device_limit_form_env`，**可能被 `NPU_DEVICE_LIMIT` 裁剪**（见 4.4）。`has_device_limit()` 正是用这二者是否相等来判断「有没有被裁剪」。

#### 4.2.4 代码实践（动手型——本讲主实践）

**目标**：用脚本打印当前设备的 arch、AI 核数、Vector 核数，亲手跑通「门面 → 单例探测器 → CANN API」这条链。

**操作步骤**：在装好 CANN、`source set_env.sh`、且有 NPU 设备的环境里，创建 `probe_npu.py`：

```python
# probe_npu.py —— 探测当前 Ascend NPU 的 arch 与核数（示例代码）
import torch_npu  # 必须先 import：注册 npu 设备、桥接 CANN 运行时（见 u2-l1）

from triton.backends.ascend.driver import NPUDriver

drv = NPUDriver()                 # 构造时会触发 NPUUtils() 现场编译 npu_utils.so（仅首次）
utils = drv.utils                 # NPUUtils 单例

print("is_active        :", NPUDriver.is_active())      # True（若 BiSheng 编译器就位）
print("target           :", drv.get_current_target())   # GPUTarget('npu', '<arch>', 0)
print("current device   :", drv.get_current_device())   # int，如 0
print("arch             :", utils.get_arch())           # SoC 名，如 'Ascend910B...'
print("AI core num      :", utils.get_aicore_num())     # 可能被 NPU_DEVICE_LIMIT 裁剪
print("Vector core num  :", utils.get_aivector_core_num())  # = AI core num × 2
print("device props     :", utils.get_device_properties("npu"))
```

运行：`python probe_npu.py`

**需要观察的现象**：
1. 首次运行会有一小段编译延迟（编译 `npu_utils.so`）；第二次起瞬完成（命中缓存）。
2. `Vector core num` 应恰为 `AI core num` 的两倍。

**预期结果**：具体数值随硬件型号变化——**待本地验证**。典型如 910B：`arch` 形如 `Ascend910B...`、AI 核数约 20、Vector 核数约 40。

> 若你的机器没有 NPU，本脚本会在 `import torch_npu` 或 `get_current_device()` 处失败——这正是 4.1 强调的：`is_active` 只看编译器，真正探测硬件必须有设备。

#### 4.2.5 小练习与答案

**练习 1**：为什么缓存键里要纳入 `version_info`（torch/torch_npu 的 git 版本）？
**答案**：`.so` 的 ABI 与 torch_npu 版本强绑定。版本一变，旧的 `.so` 可能加载失败或行为异常，纳入版本后缓存键改变、自动触发重编译，保证一致。

**练习 2**：`get_device_aicore` 为什么加 `lru_cache`，而 `get_arch` 没有？
**答案**：`rtGetAiCoreCount` 是较重的运行时调用且结果恒定，缓存收益高；`get_arch` 本身只是一次转发、开销小，且单例 + 进程内不变，加不加 cache 差别不大（这里选择不加）。

---

### 4.3 npu_utils.cpp：与 CANN runtime 对话的 C 扩展

#### 4.3.1 概念说明

`npu_utils.cpp` 是一个 **CPython 扩展模块**的源码。它定义了一张方法表（`PyMethodDef`），把若干 C 函数暴露成 Python 可调用对象。每个函数都遵循固定套路：

```text
static PyObject* 某函数(PyObject *self, PyObject *args) {
    用 PyArg_ParseTuple(args, "格式串", &c变量, ...) 把 Python 入参解析成 C 变量;
    调用 CANN 的 rt* API;
    用 Py_BuildValue("格式串", c结果) 把 C 结果组装成 Python 对象返回;
}
```

它是 `NPUUtils` 与昇腾 runtime 之间**唯一的桥梁**——Python 侧永远不直接碰 CANN C API，全部经由这个 `.so`。本讲关注其中三类函数：探测 arch、探测核数、注册内核二进制。

#### 4.3.2 核心流程

| 函数（C） | Python 名 | 内部 CANN API | 返回 |
| --- | --- | --- | --- |
| `getArch` | `get_arch` | `rtGetSocVersion(name, 64)` | SoC 名称字符串 |
| `getAiCoreNum` | `get_aicore_num` | `rtGetAiCoreCount(&cnt)` | uint32 核数 |
| `loadKernelBinary` | `load_kernel_binary` | `rtDevBinaryRegister` + `rtFunctionRegister` | (module_handle, func_handle, ...) |

#### 4.3.3 源码精读

**获取 arch**——[npu_utils.cpp:120-133](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L120-L133)：

```cpp
static PyObject *getArch(PyObject *self, PyObject *args) {
  char name[64] = {'\0'};
  rtError_t rtRet = rtGetSocVersion(name, 64);   // 向 CANN 要 SoC 名
  ...
  return Py_BuildValue("s", name);               // "s" = 返回 Python 字符串
}
```

**获取 AI 核数**——[npu_utils.cpp:135-148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L135-L148)：

```cpp
static PyObject *getAiCoreNum(PyObject *self, PyObject *args) {
  uint32_t aiCoreCnt;
  rtError_t rtRet = rtGetAiCoreCount(&aiCoreCnt); // 向 CANN 要 AI 核数
  ...
  return Py_BuildValue("I", aiCoreCnt);           // "I" = 返回 Python 无符号整型
}
```

注意：CANN 只给 **AI 核数**，**没有**「Vector 核数」API。Python 侧用 \(num_{aiv} = num_{aic} \times 2\) 自己派生出 Vector 核数（见 4.4），这是软件约定，不是硬件查询。

**注册内核二进制**——这是 driver「运行」的真正第一步（u5-l3 会用到），它把编译产物 `.o` 注册成可启动的函数句柄。[loadKernelBinary: npu_utils.cpp:94-118](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L94-L118) 用 `PyArg_ParseTuple(args, "ss#iis", ...)` 解析出 kernel 名、二进制指针、大小、shared、device、kernel_mode，然后委托 `registerKernel`。

`registerKernel`（[npu_utils.cpp:49-92](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L49-L92)）做三件事：

```cpp
devbin.data = data; devbin.length = data_size;
if (kernel_mode == "aiv")
    devbin.magic = RT_DEV_BINARY_MAGIC_ELF_AIVEC;   // aiv 用向量 ELF magic
else
    devbin.magic = RT_DEV_BINARY_MAGIC_ELF;         // 否则普通 ELF
...
rtSetDevice(device);
rtDevBinaryRegister(&devbin, &devbinHandle);        // 把 .o 注册进 CANN
...
std::string stubName = name + "_" + std::to_string(计数); // 同名多次注册时区分
rtFunctionRegister(devbinHandle, func_stub_handle, stubName.c_str(), (void*)name, 0);
return std::make_tuple(devbinHandle, func_stub_handle);   // 返回模块/函数句柄
```

`magic` 按 `kernel_mode`（aiv / aic-mix）区分，因为向量核和矩阵核的可执行格式不同（见 u2-l2 / u8-l1 的 Cube/Vector 模型）。`stubName` 加计数后缀，是为了同名 kernel 多次注册时不冲突。方法表与模块初始化见 [npu_utils.cpp:389-421](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L389-L421)。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理清 `.o` 字节如何变成可启动的函数句柄。

1. 阅读 [npu_utils.cpp:49-92](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L49-L92)，画出 `data(字节) → devbinHandle → func_stub_handle` 的三步链。
2. 回答：`loadKernelBinary` 返回的元组里，哪个句柄后续会被传给 `rtKernelLaunch`？（提示：见 [driver.py:810](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L810) 的 `func` 参数。）

**预期结果**：`func_stub_handle`（即 Python 侧 `load_kernel_binary` 返回的第 2 项）是后续启动内核用的函数句柄；`devbinHandle` 是模块句柄。`rtKernelLaunch(func, ...)` 里的 `func` 正是它。

#### 4.3.5 小练习与答案

**练习 1**：`Py_BuildValue("s", name)` 中的 `"s"` 和 `Py_BuildValue("I", cnt)` 中的 `"I"` 分别表示什么？
**答案**：`"s"` 把 C 字符串转成 Python `str`；`"I"` 把 C `unsigned int` 转成 Python `int`。这些是 CPython 的格式串约定。

**练习 2**：为什么 `registerKernel` 要给 `stubName` 加 `"_" + 计数` 后缀？
**答案**：同一个 kernel 名可能被多次注册（例如多次编译不同配置），CANN 要求函数注册名唯一，加计数后缀避免冲突；同时 `registered_names` 这个 map 记录每个名字被注册过几次。

---

### 4.4 NPU_DEVICE_LIMIT：核数裁剪机制

#### 4.4.1 概念说明

`NPU_DEVICE_LIMIT` 是一个环境变量，让用户**人为调低** Triton 看到的 NPU 核数，用途包括：多租户分片（一张卡多个任务各占一部分核）、性能实验（只用部分核对比）、资源隔离。格式为 `cube_core_num,vector_core_num`，例如 `14,28`。**不设则使用硬件真实值**（Vector 核数 = AI 核数 × 2）。

它的意义在于：前面 u2-l2 讲过，grid 直接对应物理核占用，`num_physical_blocks` 按 `mix_mode` 取 AI 核数或 Vector 核数。裁剪这两个数，就裁剪了实际并发上限。

#### 4.4.2 核心流程

`_get_npu_device_limit_form_env` 的判定流程：

```text
读 NPU_DEVICE_LIMIT
num_aic = 真实 AI 核数;  num_aiv = num_aic × 2          # 硬件上限
if 环境变量为 None: return (num_aic, num_aiv)             # 未设 → 真实值
正则校验格式 ^\d+ *, *\d+$                                 # 必须 "整数,整数"
校验两个值都 > 0                                            # 非正 → ValueError
校验 num_aic_env ≤ num_aic 且 num_aiv_env ≤ num_aiv        # 超硬件上限 → ValueError
通过 → 打印 [INFO]，返回裁剪值
```

任何一项校验失败都抛 `ValueError`（带原始输入与硬件上限），便于定位。

#### 4.4.3 源码精读

裁剪逻辑在 [_get_npu_device_limit_form_env: driver.py:77-123](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L77-L123)，核心几行：

```python
npu_device_limit_str = os.getenv("NPU_DEVICE_LIMIT")
num_aic = self.get_device_aicore()       # 真实 AI 核数（CANN API）
num_aiv = num_aic * 2                    # Vector = AI × 2（软件派生）
if npu_device_limit_str is None:
    return num_aic, num_aiv
is_valid = re.match(r'^\d+ *, *\d+$', npu_device_limit_str.strip())
...
elif num_aic_env > num_aic or num_aiv_env > num_aiv:
    raise ValueError(... 必须小于等于硬件上限 ...)
else:
    print(f"[INFO]NPU_DEVICE_LIMIT ... cube={num_aic_env},vector={num_aiv_env}")
    return num_aic_env, num_aiv_env
```

`get_device_properties` 把这两个数连同占位字段打包返回（[driver.py:132-136](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L132-L136)）：

```python
def get_device_properties(self, device):
    num_aic, num_aiv = self._get_npu_device_limit_form_env()
    return {"max_shared_mem": 1, "num_aicore": num_aic, "num_vectorcore": num_aiv}
```

> `max_shared_mem: 1` 是**占位**——代码注释写明「temporarily added to avoid triton-compiler complain」。NPU 的片上缓冲（UB）有独立机制（见 u2-l3），不走 Triton 的 `max_shared_mem` 语义。

**消费点**：裁剪后的核数最终在 `make_launcher` 里决定物理并发上限——这是把本讲与 u5-l3 串起来的关键一行（[driver.py:547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547)）：

```python
num_physical_blocks = npu_utils.get_aivector_core_num() if mix_mode == "aiv" else npu_utils.get_aicore_num()
```

即：纯向量算子（aiv）取 Vector 核数，含矩阵的算子（aic/mix）取 AI 核数——与 u2-l2 的核数分配结论完全对应。

#### 4.4.4 代码实践

**目标**：体会核数裁剪对 `num_physical_blocks` 的影响。

1. 在 4.2 的 `probe_npu.py` 基础上，先不加任何环境变量跑一次，记下 `AI core num` 与 `Vector core num`。
2. 设环境变量重跑：`NPU_DEVICE_LIMIT=10,20 python probe_npu.py`（数值需 ≤ 你的硬件上限）。
3. **观察现象**：终端会打印一行 `[INFO]NPU_DEVICE_LIMIT from env: cube_core_num=10,vector_core_num=20`，随后 `AI core num` 变 10、`Vector core num` 变 20。
4. **预期结果**：裁剪生效。若误设 `NPU_DEVICE_LIMIT=99,99`（超过硬件），应抛 `ValueError`，提示「must be less than or equal to device properties」。
5. 具体能取到多大值取决于你的硬件——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：设 `NPU_DEVICE_LIMIT=20,20`（假设硬件 AI 核数为 20），会发生什么？
**答案**：`num_aiv_env=20`，但硬件 `num_aiv = num_aic × 2 = 40`，校验 `num_aiv_env ≤ num_aiv`（20 ≤ 40）通过；`num_aic_env=20 ≤ 20` 也通过。所以合法，Vector 核被「裁」到 20（而非默认的 40）。

**练习 2**：`has_device_limit()`（[driver.py:129-130](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L129-L130)）判断的是什么？
**答案**：它比较 `get_device_aicore()`（硬件真实核数）与 `get_aicore_num()`（可能被裁剪）。二者不等说明环境变量生效、核数被裁剪了。

---

## 5. 综合实践

把本讲四个模块串起来，写一个**设备自检脚本** `npu_selfcheck.py`，一次性回答「这台机器的 Ascend 环境是否就绪、是什么型号、有多少核、是否被裁剪」：

```python
# npu_selfcheck.py（示例代码）
import os
import torch_npu
from triton.backends.ascend.driver import NPUDriver

drv = NPUDriver()
u = drv.utils

# 4.1 门面与激活判断（检测的是编译器）
print("== 激活与目标 ==")
print("is_active      :", NPUDriver.is_active())
print("target         :", drv.get_current_target())          # backend + arch + warp_size=0
print("current device :", drv.get_current_device())

# 4.2 + 4.3 经 npu_utils.so 探测硬件
print("== 硬件探测 ==")
print("arch           :", u.get_arch())
print("AI core (raw)  :", u.get_device_aicore())             # 真实，未裁剪
print("AI core (used) :", u.get_aicore_num())                # 可能裁剪
print("Vector core    :", u.get_aivector_core_num())
print("props          :", u.get_device_properties("npu"))

# 4.4 核数裁剪自检
print("== 核数裁剪 ==")
print("NPU_DEVICE_LIMIT env :", os.getenv("NPU_DEVICE_LIMIT"))
print("has_device_limit     :", u.has_device_limit())        # raw != used 即被裁剪

# 一致性断言（理解软件约定）
assert u.get_aivector_core_num() == u.get_aicore_num() * 2, "Vector 核数应等于 AI 核数 × 2"
print("OK: Vector = AI × 2")
```

**任务要求**：

1. 在真实 NPU 环境跑通它，逐行解释每个输出对应本讲的哪个模块/源码行。
2. 分别在「不设」「设合法值」「设非法值」三种 `NPU_DEVICE_LIMIT` 下运行，记录差异。
3. 用一句话回答：为什么 `is_active` 为真、但脚本仍可能在某些行报错？（答：`is_active` 只确认编译器就位，硬件探测仍需实体设备与 `torch_npu`。）

> 具体输出数值随硬件型号变化，**待本地验证**；本实践的目的是把「门面 → 单例探测器 → C 扩展 → CANN API → 核数裁剪」整条链在源码层面对上号。

## 6. 本讲小结

- `NPUDriver` 是 Ascend 后端的**运行时门面**，与编译侧 `AscendBackend` 分工：一个管「在哪跑」，一个管「怎么编」。
- core 通过 `is_active()` 在所有后端中**恰好选一个**激活驱动；`NPUDriver.is_active()` 检测的是 **BiSheng 编译器**（`bisheng -print-targets` 含 `hiipu64`），**不是** NPU 硬件——这让纯编译场景无需实体设备。
- `NPUUtils` 是**单例**，首次使用时把随包的 `npu_utils.cpp` **现场编译**成 `npu_utils.so`、按 `源码+版本哈希` 缓存、再动态加载；之后所有探测都转发给这个 `.so`。
- `npu_utils.cpp` 是 Python 与 CANN runtime 的**唯一桥梁**：`get_arch`→`rtGetSocVersion`、`get_aicore_num`→`rtGetAiCoreCount`、`load_kernel_binary`→`rtDevBinaryRegister`+`rtFunctionRegister`。
- **Vector 核数 = AI 核数 × 2** 是软件派生（CANN 无对应 API），最终在 `make_launcher` 里按 `mix_mode`（aiv 取 Vector、aic/mix 取 AI）决定 `num_physical_blocks`。
- `NPU_DEVICE_LIMIT`（`cube,vector` 格式）可裁剪核数用于分片/隔离，校验格式、正值、不超硬件上限三关；`max_shared_mem:1` 仅为占位。

## 7. 下一步学习建议

本讲打通了「设备发现与架构探测」。接下来：

- **u5-l2 NPULauncher 与 C++ launcher 代码生成**：精读 `make_launcher`，看本讲得到的核数、arch 如何被编进生成的 C++ launcher 源码（`triton_launch_kernel` / `_launch`）。
- **u5-l3 内核启动：rtKernelLaunch、workspace 与 sync_block_lock**：看 `load_kernel_binary` 返回的函数句柄如何被 `rtKernelLaunch` 在流上启动，以及 workspace / 锁的分配。
- 若想横向对比，可重读 **u2-l2**（核数分配）与 **u3-l2**（`NPUOptions` / `mix_mode`），它们与本讲的 `num_physical_blocks`、`get_current_target` 紧密呼应。
