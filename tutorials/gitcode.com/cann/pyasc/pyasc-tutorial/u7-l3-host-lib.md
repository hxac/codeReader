# Host 侧库封装：lib/host 与 tiling 辅助

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Loader` 的三步走：**查缓存 → 在线编译 → importlib 加载**，以及它为什么与 `lib/runtime` 的在线编译机制同构。
2. 理解 `ProxyMeta` 元类如何让 `wrappers.py` 里一堆「空方法体」的存根类，摇身变成直接驱动 C++ 实现的活对象。
3. 掌握 Host 侧 tiling 计算的完整链路：`get_ascendc_platform()` → `MultiCoreMatmulTiling.set_*()` → `get_tiling(tiling)` 零拷贝写回 `TCubeTiling`，以及它与 Device 侧 kernel 消费 tiling 的协作方式。
4. 解释 `bindings/*.cpp` 为什么以源码形式随 wheel 分发（`package_data`），而不是预编译成 `.so`。

## 2. 前置知识

本讲是第 7 单元第三讲，建立在前几讲概念之上，先快速回顾：

- **Host 侧与 Device 侧**（u1-l4、u7-l1）：pyasc 程序分两半。`@asc.jit` 修饰的核函数被编译成设备代码在 NPU 上跑；而 `generate_tiling` 这类普通 Python 函数在 CPU（Host）上按 Python 语义直接执行。tiling 计算（决定矩阵怎么切块、用几个核）属于 Host 侧工作。
- **TCubeTiling 是 Struct**（u3-l3、u7-l1）：`Struct` 是「三面体」——Host 侧是 ctypes 结构体（可打包成字节流）、IR 侧是 `PyStructType` 类型、设备侧经 `create_local` 得到本地副本。tiling 作为**运行时 Struct 参数**传入 kernel，改值不触发重编译。
- **在线编译模式**（u3-l7）：`lib/runtime` 把随包分发的 `rt_wrapper.cpp` 现场编译成动态库再加载，产物按 `sha256(cpp 全文 + version.cfg + 模式)` 存入文件缓存。本讲的 `lib/host` 是同一套思路的第二次应用。
- **FileCacheManager**（u3-l8）：跨进程的落盘缓存，按 key 隔离目录，原子写保证不会读到半截文件。

本讲新引入两个 Python 语言概念：

- **元类（metaclass）**：类的类。普通类的行为由其方法决定，而「类本身被调用、类的属性被访问」这两个行为由元类决定。`class Foo(metaclass=Meta)` 之后，`Foo()` 触发 `Meta.__call__`，`Foo.bar` 触发 `Meta.__getattribute__`。
- **类型存根（stub）**：方法体只写 `...`（Ellipsis）的类定义。它从不被执行，唯一作用是让 IDE 与类型检查器认识接口签名。运行期行为由别处（这里是 C++ 模块）提供。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/lib/host/loader.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/loader.py) | `Loader`：查找/编译/加载 `libhost` 扩展模块，按名取 C++ 类 |
| [python/asc/lib/host/wrappers.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/wrappers.py) | `ProxyMeta`/`ProxyBase` 元类代理 + 全部 Host 侧类的类型存根 |
| [python/asc/lib/host/\_\_init\_\_.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/__init__.py) | 汇出公开名字；提供 `get_ascendc_platform()` 便捷函数 |
| python/asc/lib/host/bindings/Module.cpp | pybind11 模块入口，聚合三个 init 函数 |
| python/asc/lib/host/bindings/MatmulApiTiling.cpp | `MatmulApiTilingBase` 及三个子类的绑定，蛇形命名映射 Ascend C 驼峰接口 |
| python/asc/lib/host/bindings/Platform.cpp | `PlatformAscendCManager.get_instance` 单例绑定 |
| python/asc/lib/host/bindings/Enums.cpp | `TPosition`/`CubeFormat`/`DataType` 等枚举绑定 |
| [python/asc/lib/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py) | 环境探测：CANN 路径、C++ 编译器、Python 头文件目录 |
| [python/asc/language/adv/tiling.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py) | 前端侧 `TCubeTiling` 等 Struct 定义（本讲的消费端） |
| [python/asc/language/core/struct.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py) | `Struct` 基类：ctypes 生成、`addressof`、JIT 分支 |
| [examples/04_matmul_cube_only/matmul_cube_only.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py) | 端到端示例：Host 算 tiling → Device 做 Matmul |
| [setup.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py) | `package_data` 把 `bindings/*.cpp` 塞进 wheel |

## 4. 核心概念与源码讲解

### 4.1 模块一：Loader——查找、在线编译与缓存加载

#### 4.1.1 概念说明

Host 侧 tiling 计算的真正实现不在 pyasc 仓库里，而在 **CANN 包的 C++ 库**（`libtiling_api`、`libplatform`、`libregister`）中。Python 无法直接调用它们，需要一个 pybind11 扩展模块当桥。这个桥——`libhost<EXT_SUFFIX>.so`——并不随 wheel 预编译分发，而是把 4 个 `.cpp` 源文件随包分发，**在用户机器上第一次用到时才编译**。

为什么要这么绕？回顾 u3-l7 的结论：pybind11 扩展模块编译时要把 C++ 类型信息写死，且依赖目标机器的 CANN 版本、Python 版本、CPU 架构；预编译产物无法在「任意 Linux + 任意 CANN 小版本」上通用。以源码形式分发 + 落地时按指纹缓存编译，是同一套问题的同一套解法——`lib/runtime` 编 `rt_wrapper.cpp` 用的是它，`lib/host` 编 `bindings/*.cpp` 用的还是它。

`Loader` 承担三件事：

1. **查**：按「模块名 + CANN 版本指纹」查文件缓存，命中则直接加载缓存里的 `.so`。
2. **编**：未命中则调用系统 C++ 编译器现场构建。
3. **载**：用 `importlib` 把 `.so` 当 Python 模块加载，并进程级记住（`Loader.module` 类属性）。

#### 4.1.2 核心流程

第一次访问 `host` 命名空间任一功能时的时序：

```text
host.MultiCoreMatmulTiling(platform)          # 用户代码
  └─ ProxyMeta.__call__                       # 4.2 讲
       └─ Loader.get_attr("MultiCoreMatmulTiling")
            └─ Loader.load_library()          # 首次触发
                 ├─ 1. Loader.module is None? 否则直接返回（进程内单例）
                 ├─ 2. key = sha256("libhost" + EXT_SUFFIX + CANN version.cfg 全文)
                 ├─ 3. cache_manager.get_file("libhost.so")
                 │      ├─ 命中 → 返回缓存路径
                 │      └─ 未命中 → Loader.build() 现场编译
                 │                   └─ 把 .so 字节写进缓存（binary=True）
                 ├─ 4. importlib 按 spec 加载模块，exec_module 执行注册逻辑
                 └─ 5. cls.module = mod      # 之后全进程复用
```

注意两层缓存的分工：`Loader.module` 是**进程内**单例（同进程第二次连 `getattr` 都省了），`FileCacheManager` 是**跨进程**落盘缓存（下次运行 Python 不必再编译）。

#### 4.1.3 源码精读

先看编译命令的组装——[python/asc/lib/host/loader.py:L25-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/loader.py#L25-L51)：

```python
@staticmethod
def build(so_name: str):
    so_path = os.path.join(os.path.dirname(__file__), so_name)
    asc_path = get_ascend_path()
    cc_cmd = [
        get_cxx_compiler(),
        os.path.join(os.path.dirname(__file__), "bindings/Platform.cpp"),
        os.path.join(os.path.dirname(__file__), "bindings/Enums.cpp"),
        os.path.join(os.path.dirname(__file__), "bindings/MatmulApiTiling.cpp"),
        os.path.join(os.path.dirname(__file__), "bindings/Module.cpp"),
        f"-I{get_py_include_dir()}",
        "-std=c++17", "-shared", "-fPIC",
        f"-I{os.path.join(asc_path, 'include')}",
        f"-I{pybind11.get_include()}",
        f"-L{os.path.join(asc_path, f'{platform.machine()}-linux/lib64')}",
        f"-L{os.path.join(asc_path, 'runtime/lib64')}",
        "-ltiling_api", "-lplatform", "-lregister",
        "-O2", "-o", so_path,
    ]
    subprocess.check_call(cc_cmd)
    return so_path
```

这段代码把 4 个随包 `.cpp` 编成共享库。四类输入值得逐一看：

- **编译器**：`get_cxx_compiler()` 优先取环境变量 `CXX`/`CC`，否则找 `g++`（有 `g++` 用 `g++`，没有才退 `clang++`），见 [python/asc/lib/utils.py:L24-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py#L24-L35)。
- **头文件**：三个 `-I` 分别指向 Python 头（`Python.h` 所在）、CANN 头（`tiling/tiling_api.h` 等）、pybind11 头。`pybind11.get_include()` 之所以可用，是因为 pybind11 是运行期依赖（[setup.py:L403-L407](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L403-L407) 中 `install_requires` 钉了 `pybind11==2.10.3`）。
- **链接库**：`-ltiling_api -lplatform -lregister` 来自 CANN 安装目录，两条 `-L` 搜索路径按 CPU 架构（`platform.machine()`）拼出。
- **前提条件**：`get_ascend_path()` 读 `ASCEND_HOME_PATH`，未设置直接抛 `EnvironmentError`，见 [python/asc/lib/utils.py:L16-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py#L16-L21)。所以 **Host tiling 功能必须有本机 CANN 包**，纯 pip 装个 pyasc 是不够的。

再看加载与缓存——[python/asc/lib/host/loader.py:L53-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/loader.py#L53-L78)：

```python
@classmethod
def load_library(cls):
    if cls.module is not None:
        return cls.module
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    suffix_key = ""
    version_cfg = get_ascend_path() / "version.cfg"
    if version_cfg.exists():
        suffix_key += version_cfg.read_text()
    so_name = f"libhost{suffix}"
    key = hashlib.sha256((so_name + suffix_key).encode("utf-8")).hexdigest()
    cache_manager = get_cache_manager(key)
    lib_host = cache_manager.get_file(so_name)
    if lib_host is None:
        so = Loader.build(so_name)
        with open(so, "rb") as f:
            lib_host = cache_manager.put(f.read(), so_name, binary=True)
    ...
    spec = importlib.util.spec_from_file_location("libhost", lib_host)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls.module = mod
    return mod
```

几个关键设计：

- **缓存 key 的构成**：`sha256("libhost.cpython-3xx-x86_64-linux-gnu.so" + CANN version.cfg 全文)`。模块名带上了 Python 扩展后缀（`EXT_SUFFIX` 编码了 Python 版本与架构），`version.cfg` 编码了 CANN 版本——任何一项变化，哈希就变，`get_cache_manager`（[python/asc/runtime/cache.py:L103-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L103-L105)）会把缓存落到不同子目录，等于自动按「Python 版本 × CANN 版本 × 架构」隔离产物。
- **编到哪、存到哪**：`build` 把 `.so` 临时产物放到 `loader.py` 旁边（包目录），随后立刻读出字节写入缓存目录；真正被 `importlib` 加载的是**缓存里的那份**。包目录下的临时 `.so` 并不会被主动清理（观察点，见下面实践）。
- **手工加载模块**：`spec_from_file_location` + `exec_module` 等价于 `import`，只是路径完全自定。执行时会跑 `PYBIND11_MODULE(libhost, m)`，把所有 C++ 类注册进这个模块对象。

最后是取类入口——[python/asc/lib/host/loader.py:L80-L85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/loader.py#L80-L85)：

```python
@classmethod
def get_attr(cls, class_name: str):
    module = cls.module
    if module is None:
        module = Loader.load_library()
    return getattr(module, class_name)
```

`get_attr` 按**字符串名字**从模块里取类。这个名字从哪来？下一节揭晓——来自 Python 代理类的 `__name__`。这就是为什么 wrappers.py 的类名与 pybind11 注册名必须逐字一致。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次「源码 → 编译 → 缓存 → 模块」的落地过程，并验证进程内单例。

**操作步骤**（需已安装 CANN 并 `source set_env.sh`，无 NPU 也可，这一层不碰设备）：

1. 记下当前缓存目录情况：`ls ~/.cache/pyasc 2>/dev/null || ls $(python3 -c "import asc.runtime.cache as c; print(c.cache_options.dir)")`（目录以实际配置为准，参考 u3-l8）。
2. 运行（示例代码，保存为 `probe_loader.py`）：

```python
# 示例代码：观察 Loader 的加载过程
from asc.lib.host import loader

print("before:", loader.Loader.module)        # 预期 None
tiling_cls = loader.Loader.get_attr("MatmulApiTilingBase")
print("after :", loader.Loader.module)        # 预期 <module 'libhost' ...>
print("class :", tiling_cls)                  # 预期 <class 'MatmulApiTilingBase'>，来自 C++ 模块
```

3. 再次运行同一脚本，比较两次耗时：第二次应当明显更快（命中文件缓存，跳过编译）。
4. 检查 `python/asc/lib/host/` 目录下是否出现了临时的 `libhost*.so` 文件。

**需要观察的现象**：

- 第一次运行有明显编译停顿，`Loader.module` 从 `None` 变为模块对象。
- 第二次运行几乎没有编译开销。
- 包目录下残留一个临时 `.so`（当前实现不清理，属观察结论）。

**预期结果**：如上。本实践依赖本机 CANN 与 C++ 编译器，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：用户把 CANN 从 8.0 升到 8.1，但没有清缓存。`Loader` 会误用旧 `.so` 吗？

**答案**：不会。缓存 key 含 `version.cfg` 全文的 sha256，CANN 升级后 `version.cfg` 内容变化，key 随之变化，`get_cache_manager` 指向新的缓存子目录，旧目录命中不了，于是重新编译。代价是旧缓存残留占磁盘。

**练习 2**：`Loader.module`（类属性）与 `FileCacheManager` 各自解决什么问题？如果只有后者会怎样？

**答案**：`FileCacheManager` 解决**跨进程**复用——避免每次启动 Python 都重新编译；`Loader.module` 解决**进程内**复用——同进程里第二次 `get_attr` 连缓存文件的 `importlib` 加载都省掉，直接 `getattr` 内存模块。只有后者的话，同一进程内每次取类都要走一遍文件加载路径（且重复 `exec_module` 加载同名模块还有额外开销）。

**练习 3**：为什么 `-ltiling_api` 等三个库必须在用户机器上存在，而不能像 `.cpp` 一样随包分发？

**答案**：它们是 CANN 商业发行的二进制，版本必须与本机 CANN 头文件、驱动栈严格匹配；tiling 计算逻辑也可能随 CANN 版本演进。绑定层（`bindings/*.cpp`）很薄且稳定，适合源码分发；实现层随 CANN 走，本机本来就有——所以分发「桥」的源码、链接本机的「实现」，各取所长。

### 4.2 模块二：ProxyMeta 代理——wrappers.py 如何「假扮」C++ 类

#### 4.2.1 概念说明

看 [python/asc/lib/host/wrappers.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/wrappers.py) 会发现一件怪事：`MatmulApiTilingBase` 定义了三十多个方法，**方法体全是 `...`**；`import asc.lib.host as host` 时明明还没编译任何东西，`host.MultiCoreMatmulTiling(platform)` 却能直接工作。

答案是：wrappers.py 里的类**从来不会被实例化**。它们是纯粹的类型存根，真正的构造、方法调用、属性访问全部被元类 `ProxyMeta` 转发给了 `Loader` 加载的 C++ 类。这套「代理」模式让 pyasc 只用维护一份 Python 签名声明，就获得了：

- IDE 补全与类型检查（存根里的类型标注是给工具看的）；
- 零手写的运行期行为（全部由 C++ 提供）;
- 惰性加载（不碰 host 功能就绝不编译 `libhost`）。

#### 4.2.2 核心流程

`ProxyMeta` 只覆写两个元类魔术方法，分别拦截「类被调用」与「类的属性被访问」：

```text
host.MultiCoreMatmulTiling(platform)         # 类被调用
  └─ ProxyMeta.__call__(cls, *args)
       ├─ cpp_class = Loader.get_attr(cls.__name__)   # 按名取 C++ 类
       ├─ instance  = cpp_class.__new__(cpp_class, *args, **kwargs)  # 创建 C++ 实例
       └─ instance.__init__(*args, **kwargs)           # 用 C++ 的构造函数
       → 返回的是 pybind11 包装的 C++ 对象，不是 wrappers.py 里的类实例！

host.TPosition.GM                            # 类属性访问
  └─ ProxyMeta.__getattribute__(cls, "GM" 之类先取 __name__ 再逐级)
       └─ getattr(cpp_class, name)           # 转发到 C++ 类同名属性
```

一个容易忽略的细节：`__getattribute__` 定义在**元类**上，所以它拦截的是「对这个类本身的属性访问」（如 `host.TPosition.GM` 里的 `.GM`，以及 `PlatformAscendCManager.get_instance` 这种静态方法访问），而**实例**的方法调用（`matmul_tiling.set_dim(24)`）不经过它——实例本来就是 C++ 对象，pybind11 已把 `set_dim` 绑定成它的方法。

#### 4.2.3 源码精读

核心只有十行——[python/asc/lib/host/wrappers.py:L16-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/wrappers.py#L16-L30)：

```python
class ProxyMeta(type):

    def __call__(cls, *args, **kwargs):
        cpp_class = Loader.get_attr(cls.__name__)
        instance = cpp_class.__new__(cpp_class, *args, **kwargs)
        instance.__init__(*args, **kwargs)
        return instance

    def __getattribute__(self, name: str):
        cpp_class = Loader.get_attr(super().__getattribute__("__name__"))
        return getattr(cpp_class, name)


class ProxyBase(metaclass=ProxyMeta):
    pass
```

逐行拆解：

- `Loader.get_attr(cls.__name__)`：用**Python 类名**当查找键。这解释了 wrappers.py 类名的「神圣性」——`MultiCoreMatmulTiling` 必须与 C++ 侧注册名（见下）一字不差，改任何一边都会 `AttributeError`。
- `cpp_class.__new__(cpp_class, ...)` + `instance.__init__(...)`：手工重演了 Python 的对象创建两步曲。因为 `cpp_class` 是 pybind11 类，这样创建出来的实例完全由 C++ 持有。
- `__getattribute__` 里先 `super().__getattribute__("__name__")` 拿类名（绕过自身避免无限递归——直接写 `self.__name__` 会再次触发 `__getattribute__`），再从 C++ 类取同名属性。

再看存根的样子——[python/asc/lib/host/wrappers.py:L254-L271](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/wrappers.py#L254-L271)：

```python
class MultiCoreMatmulTiling(MatmulApiTilingBase):

    def __init__(self, arg0) -> None:
        ...

    def get_core_num(self) -> object:
        ...
```

`...` 方法体保证这些定义合法但什么都不做。继承 `MatmulApiTilingBase` 只为类型提示（子类拥有父类接口），运行期继承链毫无作用——构造时走的是 C++ 侧 `py::class_<MultiCoreMatmulTiling, MatmulApiTilingBase>` 的真实继承。

C++ 侧注册名必须对上——[python/asc/lib/host/bindings/MatmulApiTiling.cpp:L1268-L1271](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/MatmulApiTiling.cpp#L1268-L1271)：

```cpp
py::class_<MultiCoreMatmulTiling, MatmulApiTilingBase>(m, "MultiCoreMatmulTiling", py::module_local())
    .def(
        py::init<const platform_ascendc::PlatformAscendC&>(),
        ...
```

第二个模板参数 `MatmulApiTilingBase` 声明 C++ 继承关系；字符串 `"MultiCoreMatmulTiling"` 就是 `Loader.get_attr` 的查找目标。`py::module_local()` 把类型限定在本模块内，避免与其他 pybind 模块注册同名类型时冲突。

枚举走同一条代理路径——[python/asc/lib/host/wrappers.py:L317-L334](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/wrappers.py#L317-L334) 里 `TPosition` 也是 `ProxyBase` 子类，成员 `GM`、`A1`、`VECIN` 等以 `ClassVar` 存根声明；C++ 侧由 [python/asc/lib/host/bindings/Enums.cpp:L26-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/Enums.cpp#L26-L43) 的 `py::enum_<TPosition>(m, "TPosition")` 逐值注册。于是 `host.TPosition.GM` 的完整链路是：`ProxyMeta.__getattribute__("GM")` → `Loader.get_attr("TPosition")` → `getattr(枚举类, "GM")`。

注意「三名一体」的坑：`lib/host` 的 `host.TPosition` 是 **CANN C++ 枚举的代理**，与 kernel 里用的 `asc.TPosition`（[python/asc/language/core/enums.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py)，前端 IntEnum，进 IR）是**两个不同的东西**，只是长得像。前者给 Host 侧 tiling 接口用，后者给 Device 侧代码用。

#### 4.2.4 代码实践

**实践目标**：验证「wrappers.py 的类从不被实例化，实例全部来自 C++ 模块」。

**操作步骤**（示例代码，需 CANN 环境）：

```python
# 示例代码：probe_proxy.py
import asc.lib.host as host
from asc.lib.host import wrappers

platform = host.get_ascendc_platform()
t = host.MultiCoreMatmulTiling(platform)

print(type(t))                                # 预期来自 C++ 模块，而非 wrappers
print(type(t).__module__)                     # 预期 'libhost'
print(isinstance(t, wrappers.MultiCoreMatmulTiling))   # 预期 False！
print(host.TPosition.GM, host.TPosition.GM.name, host.TPosition.GM.value)
```

**需要观察的现象**：`type(t)` 打印的类其 `__module__` 是 `libhost`；`isinstance` 检查返回 `False`——这是代理模式最反直觉的证据。

**预期结果**：如上。**待本地验证**（依赖本机 CANN）。

#### 4.2.5 小练习与答案

**练习 1**：把 `wrappers.py` 里 `class MatmulApiTiling(ProxyBase)` 改名为 `class MatmulApiTilingV2(ProxyBase)`，会发生什么？

**答案**：`host.MatmulApiTilingV2(platform)` 构造时 `Loader.get_attr("MatmulApiTilingV2")` 在 C++ 模块里找不到该名字，抛 `AttributeError`。类名是查找键，不是自由命名。反过来，`__init__.py` 的汇出名单（[python/asc/lib/host/\_\_init\_\_.py:L9-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/__init__.py#L9-L23)）也要同步维护。

**练习 2**：为什么 `ProxyMeta.__getattribute__` 里取类名要写 `super().__getattribute__("__name__")` 而不能直接写 `cls.__name__`？

**答案**：`cls.__name__` 会再次触发元类的 `__getattribute__`（属性访问拦截的是「类对象上的取属性」，`__name__` 也是属性），造成无限递归。`super().__getattribute__` 显式调用 `type` 的原始实现，绕开自身拦截。

**练习 3**：存根里 `def set_dim(self, dim: int) -> int: ...` 从不执行，那它存在的价值是什么？

**答案**：给 IDE、类型检查器和读者看。真实调用发生在 C++ 实例上（pybind11 的 `.def("set_dim", ...)` 已提供实现）。存根让 `host` 命名空间在没编译 `libhost` 时也能被 `import` 和被工具分析——惰性加载与可读性兼得。

### 4.3 模块三：tiling Host 接口——从 generate_tiling 到设备侧消费

#### 4.3.1 概念说明

u7-l1 讲过：Matmul 算子的 tiling（`single_core_m/base_m` 等切分参数）由 **Host 侧 tiling 引擎**计算，Device 侧 kernel 只消费结果。本讲补上「Host 侧怎么算」的最后一环。

三个角色：

1. **`PlatformAscendC`**：硬件平台信息单例（核数、各级 buffer 大小），tiling 引擎据此决定切块能切多大。
2. **`MultiCoreMatmulTiling` 等 tiling 对象**：CANN `matmul_tiling` 命名空间中类的代理。用户用一串 `set_*` 描述问题（矩阵形状、dtype、核数），`get_tiling()` 让引擎解出切分方案。
3. **`asc.adv.TCubeTiling`**：前端 `Struct`（u3-l3 的「三面体」）。Host 侧它是 ctypes 结构体，承装计算结果；随后作为运行时参数下发，Device 侧各核按 `get_block_idx()` 现场读字段算地址。

#### 4.3.2 核心流程

以 04 示例为例，一次 Matmul 的 Host/Device 协作全流程：

```text
【Host 侧，普通 Python 执行】
1. config.set_platform(...)                     # 选 Model/NPU（u3-l7）
2. generate_tiling(m, n, k)
   ├─ host.get_ascendc_platform()
   │    ├─ rt.get_soc_version() → config.Platform 枚举
   │    └─ PlatformAscendCManager.get_instance(soc_version.value)  # 平台单例
   ├─ matmul_tiling = host.MultiCoreMatmulTiling(platform)
   ├─ set_a_type/set_b_type/...   # 描述 A/B/C/Bias 的位置、格式、dtype、转置
   ├─ set_dim(USE_CORE_NUM) / set_org_shape / set_shape / enable_bias / set_buffer_space
   ├─ tiling = asc.adv.TCubeTiling()            # 空 ctypes 结构体
   └─ matmul_tiling.get_tiling(tiling)          # 引擎把结果直接写进 tiling 的内存
3. c = matmul_launch(a, b, bias, tiling, device)
   └─ matmul_kernel[tiling.used_core_num, stream](a, b, c, bias, tiling, workspace)
        ├─ kernel 参数打包：tiling 经 Struct.pack() 进参数 blob（u3-l6）
        └─ Host 侧读 tiling.used_core_num 决定启动核数

【Device 侧，编译出的 Kernel 代码】
4. calc_offsets(tiling, ...)                     # 每核读 tiling 字段算自己的偏移
5. register_matmul(pipe, workspace, matmul, tiling) → iterate_all（u7-l1）
```

关键时序结论：**tiling 计算发生在 Host 侧、kernel 下发之前的 `generate_tiling()` 调用里**，每个算子调用算一次；而 kernel 里对 `tiling.xxx` 的每次访问发生在**每核每次执行时**。同一份 `TCubeTiling` 类，两侧走完全不同的代码路径（见 4.3.3 第 4 点）。

#### 4.3.3 源码精读

**1）平台单例的获取**——[python/asc/lib/host/\_\_init\_\_.py:L28-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/__init__.py#L28-L30)：

```python
def get_ascendc_platform():
    soc_version = get_soc_version()
    return PlatformAscendCManager.get_instance(soc_version.value)
```

`get_soc_version` 来自 `lib/runtime`（[python/asc/lib/runtime/interface.py:L62-L63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L62-L63)，返回 `config.Platform` 枚举，注意源码中状态字段拼写就是 `soc_verison`）。`.value` 是芯片型号字符串（如 `"Ascend910B1"`），正好喂给 C++ 单例工厂——[python/asc/lib/host/bindings/Platform.cpp:L29-L34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/Platform.cpp#L29-L34)：

```cpp
.def_static(
    "get_instance",
    [](const std::string& socVersion) { return PlatformAscendCManager::GetInstance(socVersion.c_str()); },
    ret::reference, "soc_version"_a);
```

`ret::reference` 表示返回引用而非拷贝——单例语义。`PlatformAscendCManager.get_instance` 这个「静态方法调用」之所以能从 Python 直达 C++，靠的正是 4.2 的 `ProxyMeta.__getattribute__` 转发。这也再次串起 u3-l7：平台信息最终来源于运行时库的设备查询，`-v Ascend910B1` 命令行参数一路走到这里。

**2）用户侧的 tiling 描述**——[examples/04_matmul_cube_only/matmul_cube_only.py:L97-L114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L97-L114)：

```python
def generate_tiling(m, n, k) -> asc.adv.TCubeTiling:
    matmul_tiling = host.MultiCoreMatmulTiling(host.get_ascendc_platform())

    matmul_tiling.set_a_type(host.TPosition.GM, host.CubeFormat.ND, host.DataType.DT_FLOAT16, IS_TRANS_A)
    matmul_tiling.set_b_type(host.TPosition.GM, host.CubeFormat.ND, host.DataType.DT_FLOAT16, IS_TRANS_B)
    matmul_tiling.set_c_type(host.TPosition.GM, host.CubeFormat.ND, host.DataType.DT_FLOAT)
    matmul_tiling.set_bias_type(host.TPosition.GM, host.CubeFormat.ND, host.DataType.DT_FLOAT)

    matmul_tiling.set_dim(USE_CORE_NUM)
    matmul_tiling.set_org_shape(m, n, k)
    matmul_tiling.set_shape(m, n, k)
    matmul_tiling.enable_bias(ENABLE_BIAS)
    matmul_tiling.set_buffer_space(-1, -1, -1)

    tiling = asc.adv.TCubeTiling()
    matmul_tiling.get_tiling(tiling)

    return tiling
```

每一行 `set_*` 都是一次真实的 C++ 调用。以 `set_dim` 为例，C++ 侧桥接见 [python/asc/lib/host/bindings/MatmulApiTiling.cpp:L1509-L1510](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/MatmulApiTiling.cpp#L1509-L1510)——lambda 把蛇形命名的 Python 调用映射到 Ascend C 驼峰原型 `SetDim`，与 u2-l5 讲过的「pyasc 接口与 Ascend C 一一镜像」约定完全一致（Host 侧 API 同样遵守，`is_bias_in` 等参数默认值在 `.def` 中给出，所以 `set_buffer_space(-1, -1, -1)` 可以只传三个、`bt_size` 落默认值 `-1`）。

**3）零拷贝写回：get_tiling 的地址桥**——[python/asc/lib/host/bindings/MatmulApiTiling.cpp:L195-L204](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/MatmulApiTiling.cpp#L195-L204)：

```cpp
.def(
    "get_tiling",
    [](MatmulApiTilingBase& self, py::object& tiling) {
        py::object method = tiling.attr("addressof");
        py::object result = method();
        auto cpp_int = py::cast<size_t>(result);
        auto* tiling_new = reinterpret_cast<TCubeTiling*>(cpp_int);
        return self.GetTiling(*tiling_new);
    },
    "tiling"_a,
```

这是全讲最精妙的五句：C++ 不知道 `asc.adv.TCubeTiling` 是什么类型（前端 `Struct` 是纯 Python 类），但它不需要知道——它 duck-typing 地调用 Python 对象的 `addressof()` 方法拿到裸内存地址，`reinterpret_cast` 成 C++ 的 `TCubeTiling*`，然后让 tiling 引擎**直接把结果写进这块内存**。Python 侧的 `addressof` 实现于 [python/asc/language/core/struct.py:L234-L235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L234-L235)：

```python
def addressof(self) -> int:
    return int(ctypes.addressof(self.ctypes_struct))
```

能这样暴力转发的前提是**两侧内存布局逐字段对齐**：前端 `TCubeTiling` 的每个 `Field`（[python/asc/language/adv/tiling.py:L111-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py#L111-L160)）以 `name="usedCoreNum"` 等指定了与 C++ 结构体一致的成员名，全部 `int32`，且 `Struct.__init_subclass__` 统一 `_pack_ = 8`（[python/asc/language/core/struct.py:L165-L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L165-L172)）——与 u6-l4 讲过的 `DeclarePyStruct` 的 `#pragma pack(push,8)` 成对。字段名、类型、顺序、对齐四者一致，指针直写才安全。这也是「改 tiling 值不重编译」的物理基础：写的是数据内存，不是代码。

**4）同一结构体，两条读取路径**。计算完成后，Host 侧读取走 ctypes——[python/asc/language/core/struct.py:L174-L178](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L174-L178)：

```python
def __getattribute__(self, name: str) -> Numeric:
    attr = super().__getattribute__(name)
    if isinstance(attr, BaseField):
        attr = getattr(self.ctypes_struct, name)
    return attr
```

所以 [examples/04_matmul_cube_only/matmul_cube_only.py:L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L93) 的 `matmul_kernel[tiling.used_core_num, rt.current_stream()](...)` 里，`tiling.used_core_num` 是**普通 Python int**（从 ctypes 字段读出），用来决定启动多少核——这正是 u3-l1 讲的 LaunchOptions 进入口。而 kernel 体内（编译期重放时）的 `tiling.used_core_num`（如 [examples/04_matmul_cube_only/matmul_cube_only.py:L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L52)）走 [python/asc/language/core/struct.py:L193-L198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L193-L198) 的 `__getattrjit__`，生成 `emitasc.member` IR 节点，变成设备上每核的实时读取。同名访问，两个世界。

**5）bindings 的分发**。[python/asc/lib/host/bindings/Module.cpp:L24-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/Module.cpp#L24-L31) 的 `PYBIND11_MODULE(libhost, m)` 依次调用三个 init 函数（enums → matmul api tiling → platform），把所有类型注册进模块。这份 `.doc()` 与每个 `.def` 里长达几十行的 R"doc" 文档字符串（含「对应的Ascend C函数原型」与 Python 调用示例）就是 Host 侧 API 的权威文档来源——比 wrappers.py 存根详尽得多，查接口细节时应读这里。

**6）源码随包分送**。[setup.py:L399-L402](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L399-L402)：

```python
package_data={
    "asc/lib/runtime": ["rt_wrapper.cpp", "npu_utils.cpp", "print_utils.cpp"],
    "asc/lib/host": ["bindings/*.cpp"],
},
```

`package_data` 让 setuptools 把这些非 Python 文件原样打进 wheel。装好的包里**没有** `libhost*.so`——首次调用才编译（4.1）。想离线预热的用户可以在装好 CANN 的环境里手动跑一次 4.1.4 的探针脚本，让缓存就位。

#### 4.3.4 代码实践

**实践目标**：打印一次 Matmul tiling 计算的输入与输出，指认它发生的时机；再对照 loader.py 说明底层实现从哪里来。

**操作步骤**：

1. 打开 [examples/04_matmul_cube_only/matmul_cube_only.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py)，在 `generate_tiling` 的 `get_tiling` 前后各插一段打印（示例代码，改的是你本地的副本，不要提交）：

```python
# 示例代码：插入到 examples/04_matmul_cube_only/matmul_cube_only.py 的 generate_tiling 内
    tiling = asc.adv.TCubeTiling()
    print(f"[tiling] input : shape=({m},{n},{k}), dim={USE_CORE_NUM}, "
          f"a=fp16/trans={IS_TRANS_A}, b=fp16/trans={IS_TRANS_B}, c=fp32")
    ret = matmul_tiling.get_tiling(tiling)
    print(f"[tiling] get_tiling ret={ret}")          # -1 失败，非 -1 成功
    print(f"[tiling] output: used_core_num={tiling.used_core_num}, "
          f"single_core_m={tiling.single_core_m}, single_core_n={tiling.single_core_n}, "
          f"base_m={tiling.base_m}, base_n={tiling.base_n}, base_k={tiling.base_k}")
    return tiling
```

2. 运行 `python3 matmul_cube_only.py -r Model`（Model 仿真模式即可；首次运行会先编译 libhost 再编译 kernel，耗时较长）。
3. 观察打印出现的相对位置：它在 `[INFO] start process sample matmul_cube_only.` 之后、kernel 首次执行日志之前，且每次调用 `matmul_cube_only_custom` 只打印一次——这就是 Host 侧、launch 之前的时机。
4. 在同一脚本再补一行 `print(type(matmul_tiling), type(matmul_tiling).__module__)`，验证 4.2 的结论：实例来自 `libhost` 模块。
5. 回答「底层实现从哪来」：对照 [python/asc/lib/host/loader.py:L53-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/loader.py#L53-L78) 写出三行结论——查（`cache_manager.get_file`，key 含 CANN 版本）、编（`Loader.build` 用本机 g++ 编 bindings/*.cpp，链 `-ltiling_api -lplatform -lregister`）、载（`importlib` 加载 `.so` 并存入 `Loader.module`）。

**需要观察的现象**：

- `[tiling] input` 与 `[tiling] output` 成对出现，且 output 里 `used_core_num` 等字段已从 0 变为引擎解出的值（构造时 `TCubeTiling` 各字段默认 0，见 [python/asc/language/adv/tiling.py:L112-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/tiling.py#L112-L160) 的 `default=0`）。
- `type(matmul_tiling).__module__` 为 `libhost`。
- 把 `USE_CORE_NUM` 改成 12 再跑：input 行变，output 的切分字段随之变化；由于 04 示例装饰器写了 `always_compile=True`（[examples/04_matmul_cube_only/matmul_cube_only.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L31)），kernel 会重编——但请想清楚：**即使去掉 always_compile，改 USE_CORE_NUM 也不该触发重编译**，因为 tiling 是运行时 Struct 参数，只进缓存 key 的「类型」不进「值」（u3-l3、u3-l8）。

**预期结果**：M=128、K=64、N=30720 的输入解出一组非零切分（具体数值由 tiling 引擎按平台与约束决定，以实际输出为准），最终 `torch.allclose` 断言通过。本实践需 CANN 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`get_tiling` 的 C++ 桥接为什么敢对 Python 对象直接 `reinterpret_cast`？如果前端把 `TCubeTiling` 的某个字段从 `int32` 改成 `int64`，会发生什么？

**答案**：因为两侧结构体按「字段名 + 类型 + 顺序 + `_pack_=8` 对齐」逐字段镜像，`addressof` 指向的 ctypes 内存与 C++ `TCubeTiling` 布局一致。若单方面把一个字段改成 `int64`，后续所有字段偏移错位，引擎写入的数据会张冠李戴——不会报错，但读到的是垃圾值，属于典型的「静默内存布局错配」事故。所以前端 tiling.py 与 C++ 头文件必须同步修改。

**练习 2**：`host.TPosition.GM` 与 kernel 里 `asc.TPosition.GM` 是同一个对象吗？各自通向哪里？

**答案**：不是。前者经 `ProxyMeta` 代理到 CANN C++ 枚举 `matmul_tiling::TPosition`，只用于 Host 侧 tiling 描述（`set_a_type` 的第一个参数）；后者是前端 IntEnum（u2-l4），在 JIT 编译期转成 IR 枚举属性，用于 Device 侧代码。二者值域高度相似但类型系统完全独立。

**练习 3**：如果一台纯 CPU 的机器（装了 CANN 但无 NPU）跑 04 示例的 `generate_tiling`，会走到哪一步？哪一步会失败？

**答案**：`generate_tiling` 本身可以走通——`get_ascendc_platform` 只需运行时库返回的芯片型号（Model 模式下由仿真器提供），tiling 计算是纯 CPU 运算。失败发生在后续 kernel 执行需真机或仿真器资源时。但若机器连 CANN 都没装（无 `ASCEND_HOME_PATH`），`Loader.build` 前的 `get_ascend_path()` 就会抛 `EnvironmentError`，`libhost` 根本编不出来。

## 5. 综合实践

**任务：给你的 Matmul 加一个「tiling 观测器」，产出一份 Host 侧决策报告。**

基于 04 示例完成：

1. **改造 generate_tiling**：按 4.3.4 插入输入/输出打印；再补一行调用 `matmul_tiling.get_core_num()` 与 `get_single_shape()`（接口见 [python/asc/lib/host/bindings/MatmulApiTiling.cpp:L1356-L1366](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/MatmulApiTiling.cpp#L1356-L1366) 与 L1407-L1417，两者都需在 `get_tiling` 之后调用，返回元组或 `None`），把 tiling 引擎实际采用的 `(dim, m_dim, n_dim)` 与单核形状也打出来。
2. **做两组对照实验**：固定 `m, k = 128, 64`，让 `n` 分别取 30720 与 4096；再固定 `n=30720`，让 `USE_CORE_NUM` 取 24 与 12。记录四组 `(输入, used_core_num, single_core_m, single_core_n, base_m, base_n)`。
3. **验证「值不进缓存」**：删掉装饰器里的 `always_compile=True`（先读懂 [examples/04_matmul_cube_only/matmul_cube_only.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/04_matmul_cube_only/matmul_cube_only.py#L31)），连续两次用不同 `USE_CORE_NUM` 运行，用日志时间或 `PYASC_DUMP_PATH` 是否新增 `ascendc.cpp` 判断第二次是否重编译；解释与 u3-l8 缓存 key 五要素的关系。
4. **写结论**：用三句话回答——tiling 在哪个侧、哪个时机算出；结果通过什么机制（`addressof` + `reinterpret_cast`）流回 Python；kernel 侧又通过什么机制（`emitasc.member`）逐核读取。

产出物是一份包含打印日志摘录、四组对照数据表、缓存行为结论的短报告。全程只改示例脚本与新增打印，不触碰 pyasc 源码。

## 6. 本讲小结

- `Loader` 用「查文件缓存 → 本机 g++ 现场编译 bindings/*.cpp（链 CANN 的 tiling_api/platform/register）→ importlib 加载」三步，把 CANN 的 Host 侧 tiling 引擎接进 Python；缓存 key 含扩展后缀与 CANN `version.cfg` 全文，天然按 Python/CANN 版本隔离，进程内再以 `Loader.module` 单例复用。
- `wrappers.py` 是纯类型存根：`ProxyMeta` 元类的 `__call__`/`__getattribute__` 把构造与属性访问按**类名**转发给 C++ 类，实例全是 pybind11 对象；类名因此是跨语言查找键，不可随意改。
- `get_tiling` 通过「Python `addressof()` → `reinterpret_cast<TCubeTiling*>` → 引擎直写内存」实现零拷贝写回，安全性完全依赖前端 Struct 与 C++ 结构体的字段名/类型/顺序/`_pack_=8` 四重对齐。
- 同一个 `TCubeTiling`：Host 侧经 `__getattribute__` 读 ctypes 字段（普通 int），Device 侧经 `__getattrjit__` 生成 `emitasc.member`（每核运行时读取）；tiling 是运行时参数，改值不重编译。
- `package_data` 让 `bindings/*.cpp` 以源码形态随 wheel 分发——绑定层薄而稳定适合带源码，实现层厚且版本敏感留给本机 CANN，与 `lib/runtime` 的 `rt_wrapper.cpp` 策略一致。

## 7. 下一步学习建议

- **u7-l4（调试与调优）**：tiling 参数直接决定性能。学会用 `PYASC_DUMP_PATH`、`print_ir_before_all` 与 msprof 观察不同 tiling 下 kernel 的执行差异，把本讲的对照实验升级为调优实验。
- **u7-l6（测试体系与贡献流程）**：若想给 `lib/host` 增加一个新的 Host 侧 Ascend C 类代理，需要同时动 `bindings/*.cpp`（pybind 注册）、`wrappers.py`（存根）、`__init__.py`（汇出）三处——正好用第 7 单元末讲的贡献 checklist 演练一遍。
- **延伸阅读源码**：对照 [python/asc/lib/runtime/build_utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py)（u3-l7 讲过）与本讲 loader.py，体会「随包源码 + 在线编译 + 指纹缓存」这一模式在两个子包中的同构实现；再读 [python/asc/lib/host/bindings/MatmulApiTiling.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/host/bindings/MatmulApiTiling.cpp) 中任意三个接口的 R"doc" 文档串，那里是 Host 侧 API 最完整的用法权威。
