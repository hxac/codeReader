# A5 仿真运行（camodel）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚在没有真实 A5 NPU 时，tilelang-ascend 是如何用 **camodel（节拍精确模型）** 在普通 x86/Linux 机器上把同一个 kernel「假装」跑起来的。
- 掌握仿真运行的核心开关：`TL_RUN_MODE=sim` 与 `TL_PLATFORM=A5`，以及它们如何在编译期把 `-lruntime` 换成 `-lruntime_camodel`。
- 读懂一条命令跑通仿真的脚本 `run_a5_sim_template.py`，并理解其中的「换库 → 重执行 → rtMalloc → kl.call」全链路。
- 知道仿真的边界：为什么只能用 PTO 后端、为什么不支持 `torch.npu`、为什么不能拿它测性能。
- 会用 `/tilelang-a5-sim-convert` skill 把任意一个现有 example 脚本转成仿真脚本。

## 2. 前置知识

本讲是实战单元的「工具箱」一讲，承接两篇前置讲义：

- **u6-l4 运行时加载与 Bisheng 设备编译**：那里讲清了「源码 → bisheng 编译 `.so` → ctypes 加载 → 调用 `call` 符号」这条真实运行链路。本讲的核心就是在这条链路里做一处「换库」操作，所以你需要先理解 host 侧 `call` 函数和 `<<<core, nullptr, stream>>>` 启动约定。
- **u7-l4 调试与性能分析**：那里讲过 `get_kernel_source()`、`msprof op/simulator` 等调试手段。本讲补充的是「连板子都没有」时如何验证 kernel 正确性、如何拿到指令级 trace。

你还需要记住一个事实：PTO 与 ascendc 是两条 codegen 路线（见 u6-l2），它们最终都链接 `-lruntime` 这个 CANN 提供的运行时库。本讲的全部「魔法」都建立在这个链接名之上。

> 术语速查：
> - **camodel**：Cycle-Accurate Model，CANN 提供的「节拍精确模型」软件模拟器，用 CPU 模拟 NPU 的硬件行为。
> - **`libruntime.so` / `libruntime_camodel.so`**：真实运行时库与仿真运行时库，对外接口完全一致，区别只在「真发指令给硬件」还是「用 CPU 算出结果」。
> - **RT API**：`rtMalloc` / `rtMemcpy` / `rtStreamCreate` 等运行时函数，是 `torch.npu` 在底层调用的同一套接口。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/jit/adapter/libgen.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py) | `LibraryGenerator`，调用 bisheng 编译 `.so`；**仿真的核心改动就在这里**——把 `-lruntime` 替换为 `-lruntime_camodel`。 |
| [.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py) | 一条命令跑通仿真的样板脚本，260 行，自动完成环境设置、换库、编译、启动、验证。 |
| [.agents/skills/tilelang-a5-sim-convert/scripts/parse_example.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/parse_example.py) | 解析任意 example 脚本，输出 kernel 的 buffer shape/dtype 的 JSON，供转换脚本使用。 |
| [.agents/skills/tilelang-a5-sim-convert/SKILL.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/SKILL.md) | `/tilelang-a5-sim-convert` skill 的说明文档，定义「把已有脚本转成仿真版」的逐项改动清单。 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 第 2.4 节「A5 Camodel 仿真运行」是本讲对应的官方文档，含原理、环境变量表、局限性。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **camodel 仿真原理与 `libruntime_camodel` 替换机制**（核心）
2. **仿真环境变量体系：`TL_RUN_MODE` / `TL_PLATFORM` 与「重执行」**
3. **`run_a5_sim_template.py`：一条命令跑通全流程**
4. **把已有脚本转成仿真版（`/tilelang-a5-sim-convert`）与仿真局限**

---

### 4.1 camodel 仿真原理与 libruntime_camodel 替换机制

#### 4.1.1 概念说明

真实 NPU 上跑 kernel 的链路是（回顾 u6-l4）：

```
DSL 定义 → JIT 编译 .so → torch.npu 分配 NPU 内存 → 真实 NPU 执行
```

这里有个关键前提：你得**有一块真实 A5 NPU**。但 A5 是较新的硬件，开发机上常常没有。camodel 解决的就是「没有硬件也要能跑」：

```
DSL 定义 → 链接 libruntime_camodel.so 编译 → rtMalloc 分配模拟内存 → CPU 模拟 NPU 执行
```

`camodel` 全称 **Cycle-Accurate Model（节拍精确模型）**，是 CANN 自带的软件模拟器：它用 CPU 模拟 NPU 的硬件行为，对外暴露的接口和真实运行时库**一模一样**。换句话说，你的 `.so` 不用改一行代码，只是把背后那个「真正干活」的库从硬件驱动换成了 CPU 模拟。

> 为什么叫「节拍精确」？因为它不只算出对不对，还能模拟出每条指令在硬件流水线上的时序，从而能 dump 出每个 core 的指令日志、UB 读写日志。不过「节拍精确」指的是功能/时序保真，**不代表它的执行速度接近真实硬件**——恰恰相反，它比真实硬件慢约 1000 倍（见 4.4 节）。

#### 4.1.2 核心流程

「换库」发生在编译期（`compile_lib`），运行期（`load_lib`）也要让动态链接器优先找到仿真库：

```
┌──────────────── 编译期 compile_lib() ────────────────┐
│ 1. 读 TL_RUN_MODE，若 == "sim" 进入仿真分支          │
│ 2. _get_simulator_lib_path() 找到 camodel 库目录     │
│ 3. 在命令里插入 -L{sim_lib} -Wl,-rpath,{sim_lib}     │
│              -Wl,--disable-new-dtags（排在 lib64 前）│
│ 4. 把 "-lruntime" 替换成 "-lruntime_camodel"          │
│ 5. bisheng 照常编译，产出 .so（内部依赖仿真库）       │
└──────────────────────────────────────────────────────┘
┌──────────────── 运行期 load_lib() ───────────────────┐
│ 1. 读 TL_RUN_MODE，若 == "sim"                       │
│ 2. 把 camodel 库目录插到 LD_LIBRARY_PATH 最前        │
│ 3. ctypes.CDLL(.so) —— dlopen 时优先解析到仿真库     │
└──────────────────────────────────────────────────────┘
```

整个机制没有任何「特殊代码路径」——kernel 的 `.so` 内容和真实运行时**完全相同**，只是它链接的运行时库变了。这是本讲最重要的设计直觉：**camodel 仿真 = 同一份代码 + 换一个运行时库**。

#### 4.1.3 源码精读

替换的入口在 `LibraryGenerator.compile_lib()` 的末尾，紧跟在 ascendc/pto 两条命令构造完之后。这段代码对两条路线**通用**：

[tilelang/jit/adapter/libgen.py:230-254](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L230-L254) —— 仿真分支：插入仿真库路径并把 `-lruntime` 改名为 `-lruntime_camodel`：

```python
# --- camodel (simulator) support ---
run_mode = os.environ.get("TL_RUN_MODE", "npu")
if run_mode == "sim":
    sim_lib_path = _get_simulator_lib_path(ASCEND_HOME_PATH, self.platform)
    # 在 -L.../lib64 之前插入仿真库路径，
    # 让 libruntime_camodel.so 优先于 libruntime.so
    try:
        ascend_lib_idx = command.index(f"-L{ASCEND_HOME_PATH}/lib64")
        command.insert(ascend_lib_idx, f"-L{sim_lib_path}")
        command.insert(ascend_lib_idx + 1, f"-Wl,-rpath,{sim_lib_path}")
        command.insert(ascend_lib_idx + 2, "-Wl,--disable-new-dtags")
    except ValueError:
        command.insert(1, f"-L{sim_lib_path}")
        ...
    # 把 '-lruntime' 替换成 '-lruntime_camodel'
    try:
        rt_idx = command.index("-lruntime")
        command[rt_idx] = "-lruntime_camodel"
    except ValueError:
        pass
    logger.info("camodel sim mode: using %s", sim_lib_path)
```

注意这里有两道**保险**，二者缺一不可：

1. **替换 `-lruntime`**（[libgen.py:248-251](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L248-L251)）：这是「指名道姓」——直接把链接名改掉，确保 `.so` 在链接期就绑定到仿真库。
2. **`-Wl,--disable-new-dtags`**（[libgen.py:236-243](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L236-L243)）：这是个容易被忽略的细节。默认情况下 `-rpath` 写入的是 `DT_RUNPATH`（**非传递**）——即链接器只会用 rpath 找 `.so` 自己直接依赖的库，但**不会**用 rpath 去找这些依赖库的依赖。而 `libruntime_camodel.so` 自己又依赖 `libnpu_drv_camodel.so` 等一堆仿真驱动库，如果用 `DT_RUNPATH`，这些二级依赖就找不到。加上 `--disable-new-dtags` 后，rpath 变成老的 `DT_RPATH`（**传递**），整套仿真库依赖链都能被解析。

被替换的 `-lruntime` 在两条 codegen 命令里都存在：ascendc 路线在 [libgen.py:173](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L173)，pto 路线在 [libgen.py:217](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L217)。所以从「改命令」的角度，ascendc 与 pto 都能被替换；但**实际只有 pto 能真正在仿真器上跑起来**，原因见 4.4 节的局限说明。

camodel 库目录的查找由辅助函数完成。[tilelang/jit/adapter/libgen.py:52-78](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L52-L78)：对 `platform=="A5"` 依次尝试 `Ascend950PR_9599`、`Ascend910_9599` 两个 SoC 目录，并刻意取 `lib/` 子目录（而非 `camodel/`）——因为 `lib/` 里才带有 `config.json` 与 `*.toml` 配置，缺这些配置 camodel 会崩在 `TMultiRing`：

```python
def _get_simulator_lib_path(ascend_home, platform):
    sim_base = os.path.join(ascend_home, "tools", "simulator")
    if not os.path.isdir(sim_base):
        sim_base = os.path.join(ascend_home, "simulator")
    if platform == "A5":
        soc_candidates = ["Ascend950PR_9599", "Ascend910_9599"]
    else:
        soc_candidates = ["Ascend910B1", "Ascend910_9599"]
    for soc in soc_candidates:
        soc_dir = os.path.join(sim_base, soc)
        ...
        candidate = os.path.join(soc_dir, "lib")
        if os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError(...)
```

运行期的 `load_lib()` 也要配套把仿真库目录排到 `LD_LIBRARY_PATH` 最前，否则 `dlopen` 仍可能解析到真实库。[tilelang/jit/adapter/libgen.py:130-140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L130-L140)：

```python
def load_lib(self, lib_path=None):
    ...
    run_mode = os.environ.get("TL_RUN_MODE", "npu")
    if run_mode == "sim":
        ascend_home = _get_ascend_home_path()
        sim_lib_path = _get_simulator_lib_path(ascend_home, self.platform)
        ld_path = os.environ.get("LD_LIBRARY_PATH", "")
        if sim_lib_path not in ld_path.split(":"):
            os.environ["LD_LIBRARY_PATH"] = f"{sim_lib_path}:{ld_path}"
    return ctypes.CDLL(lib_path)
```

#### 4.1.4 代码实践

**实践目标**：用「肉眼」验证「换库」确实只改了链接命令，没碰 kernel 源码。

**操作步骤**：

1. 准备一个最小 kernel 源码（任意 `prim_func` 经 `tilelang.lower` 产出的 C++）。
2. 在 Python 里构造 `LibraryGenerator(target="pto", platform="A5")`，`update_lib_code(src)` 填入源码。
3. **不设** `TL_RUN_MODE`，调用 `compile_lib()`，记录下生成的 `.so`（路径在 `libgen.get_lib_path()`）。
4. 设 `os.environ["TL_RUN_MODE"] = "sim"`、`os.environ["TL_PLATFORM"] = "A5"`，再次 `compile_lib()`，记录第二份 `.so`。
5. 用 `ldd` 对比两份 `.so`：`ldd <so1>` 与 `ldd <so2>`，重点看 `libruntime` 那一行解析到了哪个文件。

**需要观察的现象**：第一份 `.so` 的 `ldd` 输出里是 `libruntime.so => .../lib64/libruntime.so`；第二份会变成 `libruntime_camodel.so => .../simulator/Ascend950PR_9599/lib/libruntime_camodel.so`，并且会多出一串 `libnpu_drv_camodel.so` 等仿真驱动依赖（这正是 4.1.3 里 `--disable-new-dtags` 要保证能被解析到的那些）。

**预期结果**：两份 `.so` 的设备代码完全一致，差异仅在链接的运行时库。**待本地验证**（需要本机已装带 `Ascend950PR_9599` 镜像的 CANN 9.0.0+）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `compile_lib()` 里的 `command[rt_idx] = "-lruntime_camodel"` 这行删掉，但保留 `-L/-rpath` 的插入，仿真还能跑通吗？为什么？

> **答案**：通常跑不通。仅插入路径而不改名，链接器仍会按 `-lruntime` 去找 `libruntime.so`，在 `lib64` 里找到真实运行时库并绑定，于是 `.so` 启动时仍调真实驱动，在没有硬件的机器上会失败。`-rpath` 只决定「去哪找」，`-l` 名字才决定「找谁」，二者必须同时改。

**练习 2**：为什么 camodel 库路径必须插在 `-L.../lib64` **之前**，而不是之后？

> **答案**：链接器按 `-L` 出现的先后顺序搜索。`lib64` 里同时存在 `libruntime.so`（真实）和仿真库目录里的 `libruntime_camodel.so`——虽然名字不同，但 camodel 自身依赖的其他库（如某些同名配置库）可能与 `lib64` 重名。把仿真路径排前，保证所有同名库都优先取仿真版本，避免「半真半仿真」的混搭。

---

### 4.2 仿真环境变量体系：TL_RUN_MODE / TL_PLATFORM 与「重执行」

#### 4.2.1 概念说明

4.1 节里反复出现的 `TL_RUN_MODE`、`TL_PLATFORM`，加上几个辅助变量，构成了仿真运行的「环境变量体系」。它们的作用分工很清晰：

| 变量 | 取值 | 作用 |
|------|------|------|
| `TL_RUN_MODE` | `sim` / `npu`（默认） | 总开关。`sim` 触发 4.1 节的换库分支；不设或 `npu` 时一切走真实链路。 |
| `TL_PLATFORM` | `A5` 等 | 指定目标平台，决定 `_get_simulator_lib_path` 找哪个 SoC 目录、决定 pto codegen 的 `--cce-aicore-arch`（`dav-c310`）等。 |
| `LD_LIBRARY_PATH` | 含 `<CANN>/tools/simulator/Ascend950PR_9599/lib` | 让动态链接器优先找到 camodel 库。 |
| `TORCH_DEVICE_BACKEND_AUTOLOAD` | `0` | 阻止 `torch_npu` 在 import 时自动初始化设备——camodel 环境下这会失败。 |
| `CAMODEL_LOG_PATH` | 任意目录 | camodel 日志输出目录，里面是 instruction trace、UB dump 等分析素材。 |

这张表对应官方文档 [docs/TileLang-Ascend Programming Guide.md:256-266](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L256-L266)。

> 注意一个关键细节（文档也强调了）：`TL_PLATFORM=A5` **只在 `TL_RUN_MODE=sim` 时才生效**，它不会影响对真实 NPU 硬件的检测。换句话说，就算你在装了真硬件的机器上误设了 `TL_PLATFORM`，只要 `TL_RUN_MODE` 不是 `sim`，就完全走真实链路。这是一个避免「误伤」的设计。

#### 4.2.2 核心流程

环境变量不是「设了就生效」那么简单——这里有个 Python 仿真脚本的经典坑：**`LD_LIBRARY_PATH` 在进程启动后修改，对 `dlopen` 不可靠**。

Linux 的动态链接器（`ld.so`）只在进程**启动时**读一次 `LD_LIBRARY_PATH`。脚本运行后用 `os.environ["LD_LIBRARY_PATH"] = ...` 修改，并不会让之后 `ctypes.CDLL` 的搜索路径随之更新（`dlopen` 看的是进程初始环境）。因此样板脚本采用了一个「自重执行（re-exec）」技巧：

```
┌─ 第一次启动 ──────────────────────────────────────┐
│ 1. setup() 在 Python 里设好所有环境变量           │
│    （含 LD_LIBRARY_PATH、TL_RUN_MODE …）          │
│ 2. 检测到 _A5_SIM_REEXEC 未设 → 用 os.execve       │
│    把当前进程替换成「带新环境的自己」再跑一遍      │
└───────────────────────────────────────────────────┘
            │ os.execve（原地替换，PID 不变）
            ▼
┌─ 第二次启动（环境已是仿真配置）────────────────────┐
│ 1. _A5_SIM_REEXEC 已设 → 不再重执行                │
│ 2. 正常 import tilelang / 加载 camodel 库          │
│    此时 dlopen 能正确解析到仿真库                  │
└───────────────────────────────────────────────────┘
```

`os.execve` 是「原地替换」：用新的环境变量数组重新执行同一个脚本，PID 不变，但进程的初始环境被刷新了，于是 `dlopen` 也能看到新的 `LD_LIBRARY_PATH`。这是在 Python 里处理「需要先改环境再 import」这类依赖顺序问题的标准做法。

#### 4.2.3 源码精读

环境变量全部在 `run_a5_sim_template.py` 的 `setup()` 里集中设置，且**必须早于任何 `import tilelang` / `import torch`**：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:68-97](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L68-L97) —— 定位 CANN、source 其环境、把仿真库排到 `LD_LIBRARY_PATH` 前、设四个仿真专用变量、必要时重执行：

```python
def setup(log_dir=None):
    ascend_home = _find_ascend_home()
    os.environ["ASCEND_HOME_PATH"] = ascend_home
    _source_cann(ascend_home)                 # source setenv.bash 捕获其导出的变量

    sim_lib = _find_sim_lib(ascend_home)
    lib64 = os.path.join(ascend_home, "x86_64-linux", "lib64")
    ld = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [sim_lib, lib64] + [p for p in ld.split(":") if p and p not in (sim_lib, lib64)]
    os.environ["LD_LIBRARY_PATH"] = ":".join(paths)

    os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"   # 阻止 torch_npu 自动初始化
    os.environ["TL_RUN_MODE"] = "sim"                   # 总开关
    os.environ["TL_PLATFORM"] = "A5"                    # 目标平台

    if log_dir is None:
        log_dir = os.path.join(os.getcwd(), "camodel_log")
    os.makedirs(log_dir, exist_ok=True)
    os.environ["CAMODEL_LOG_PATH"] = log_dir            # trace/UB dump 输出目录

    # 若本进程启动时 LD_LIBRARY_PATH 还没设好，就重执行一次自己
    if "_A5_SIM_REEXEC" not in os.environ:
        os.environ["_A5_SIM_REEXEC"] = "1"
        os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)
    ...
```

注意几个值得细看的点：

- **`_source_cann`**（[run_a5_sim_template.py:40-56](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L40-L56)）：CANN 的环境变量通常由 `bin/setenv.bash` 设置。脚本在子 shell 里 `source` 它再 `env`，把导出的变量逐行捞回当前进程——这样就不用要求用户在 shell 里先 source。
- **`_find_sim_lib`**（[run_a5_sim_template.py:58-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L58-L65)）：与 `libgen.py` 里的 `_get_simulator_lib_path` 是同一个查找逻辑（A5 → `Ascend950PR_9599`），只是在 Python 侧提前算好用于设 `LD_LIBRARY_PATH`。
- **`_A5_SIM_REEXEC` 哨兵**（[run_a5_sim_template.py:90-92](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L90-L92)）：用一个自定义环境变量做「是否已重执行」的标志，避免无限循环。

为什么 `TL_RUN_MODE` / `TL_PLATFORM` 的读取点在 C++ 侧？回顾 4.1.3，`libgen.py` 里 `os.environ.get("TL_RUN_MODE", "npu")` 直接读 Python 进程环境。而 `TL_PLATFORM` 除了影响 `_get_simulator_lib_path`，还影响 pto codegen 的架构选择——[tilelang/jit/adapter/libgen.py:185-186](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L185-L186)：

```python
ccec = "dav-c310" if self.platform == "A5" else "dav-c220"
memory = "REGISTER_BASE" if self.platform == "A5" else "MEMORY_BASE"
```

也就是说，`platform="A5"` 会让 bisheng 以 A5 的核架构（`dav-c310`）和寄存器基址约定（`REGISTER_BASE`）来编译，这是 pto 代码能在 A5 仿真器上跑起来的另一个必要条件。

#### 4.2.4 代码实践

**实践目标**：亲手验证「在 Python 里改 `LD_LIBRARY_PATH` 不生效，必须重执行」这个坑。

**操作步骤**：

1. 写一个最小脚本，先 `import ctypes`，然后 `os.environ["LD_LIBRARY_PATH"] = "/some/path"`，再 `ctypes.CDLL("libruntime_camodel.so")`。
2. 直接运行——大概率报 `libruntime_camodel.so: cannot open shared object file`。
3. 把同一份 `LD_LIBRARY_PATH` 放到 shell 启动前（`LD_LIBRARY_PATH=/some/path python script.py`），再运行——这次能加载。

**需要观察的现象**：进程内改环境变量，`dlopen` 仍按进程启动时的环境找库。

**预期结果**：验证了 `run_a5_sim_template.py` 里 `os.execve` 重执行的必要性。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`setup()` 里为什么不直接在脚本最顶端 `import tilelang`，而要把 `import tilelang` 放到 `setup()` 之后（见主流程第 192-193 行）？

> **答案**：`tilelang` 在 import 时会触发一系列副作用（注册 target、可能间接 import torch/torch_npu）。如果在环境变量设好之前 import，`TL_RUN_MODE`/`TORCH_DEVICE_BACKEND_AUTOLOAD` 还没就位，编译路径就会走真实链路、或 torch_npu 抢先初始化而失败。所以必须先 `setup()`（含可能的 `os.execve`）把环境配好，再 import——这也是 4.2.2「重执行」要解决的依赖顺序问题。

**练习 2**：`TL_PLATFORM=A5` 在「装了真实 A5 硬件」的机器上设了，会不会影响真实运行？

> **答案**：不会。只要 `TL_RUN_MODE != "sim"`，`compile_lib` 和 `load_lib` 的仿真分支都不会进入，`TL_PLATFORM` 只在仿真分支里被 `_get_simulator_lib_path` 消费。真实硬件检测走的是另一条独立通道。文档第 2.4.5 节也明确「仅在 `TL_RUN_MODE=sim` 时生效」。

---

### 4.3 run_a5_sim_template.py：一条命令跑通全流程

#### 4.3.1 概念说明

4.1 讲了「换库」，4.2 讲了「环境变量」，但真要手工把这些拼起来很容易出错。于是仓库提供了一个**样板脚本** `run_a5_sim_template.py`（260 行），把仿真运行的所有步骤打包成一条命令：

```bash
python .agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py
```

成功时会打印 `KERNEL OUTPUT MATCH!` 与 `Done!`。它的设计哲学是「**一条命令、零手工配环境**」：所有 `TL_RUN_MODE`、`LD_LIBRARY_PATH`、重执行逻辑都封装在内部，用户只需保证 CANN 已装、`bisheng` 可用、`tilelang` 可 import。

#### 4.3.2 核心流程

脚本的主流程 `main()` 分成 8 个阶段，是一条完整的「环境 → 编译 → 运行 → 验证」链路：

```
0. setup()         自动环境设置 + 必要时 os.execve 重执行
1. load_runtime()  dlopen libruntime_camodel.so，绑定 rtSetDevice/rtMalloc/rtMemcpy...
2. import tilelang 此时环境已就绪，安全 import
3. KERNELS[k]()    生成 prim_func（make_gemm）
   tilelang.lower(prim_func, target="pto", platform="A5")  → 产出 C++ 源码
4. LibraryGenerator(target="pto", platform="A5").compile_lib()  → bisheng 编 .so
5. ctypes.CDLL(so) 加载 .so，绑定 kl.call
6. 准备数据         numpy 造输入 → rtMalloc 分配模拟显存 → rtMemcpy(H2D)
7. kl.call(d_A,d_B,d_C,stream) + rtStreamSynchronize + rtMemcpy(D2H)
8. np 验证          abs(got - ref) 与参考结果比对，打印 MATCH / FAILED
```

注意一个与真实运行（u6-l4）的**根本差异**：真实运行时数据用 `torch.npu` 张量管理（`a.npu()` 自动分配显存）；仿真时**没有 `torch.npu`**，所有显存操作都得退回到最底层的 **RT API**——`rtMalloc` 分配、`rtMemcpy` 在 host/device 间拷贝。这是 4.4 节「不支持 torch.npu」这条局限的直接体现。

#### 4.3.3 源码精读

**加载仿真运行时**。`load_runtime()` 用全路径 `dlopen` 仿真库，并逐个声明 RT API 的参数/返回类型——这是用 ctypes 调 C 库的常规姿势：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:105-129](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L105-L129)：

```python
def load_runtime(sim_lib):
    sys.setdlopenflags(os.RTLD_LAZY | os.RTLD_GLOBAL)
    # 用全路径加载——Python 内改 LD_LIBRARY_PATH 对 dlopen 不可靠
    rt_path = os.path.join(sim_lib, "libruntime_camodel.so")
    rt = ctypes.CDLL(rt_path)
    rt.rtSetDevice.argtypes = [ctypes.c_int32];       rt.rtSetDevice.restype = ctypes.c_int
    rt.rtMalloc.argtypes  = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint64,
                             ctypes.c_int, ctypes.c_uint16]
    rt.rtMemcpy.argtypes  = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
                             ctypes.c_uint64, ctypes.c_int]
    rt.rtStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int32]
    ...
    if rt.rtSetDevice(0) != 0:
        raise RuntimeError("rtSetDevice(0) failed")
    return rt
```

这里的 `rtMalloc`/`rtMemcpy`/`rtStreamCreate` 就是 `torch.npu` 在底层调用的同一套 CANN 运行时接口——camodel 版的它们会在 CPU 上模拟出「设备内存」和「流」。`rtMemcpy` 最后一个参数 `1` 表示 host→device、`2` 表示 device→host（见主流程里的 H2D/D2H 调用）。

**编译链路**。主流程里直接调用 `tilelang.lower` + `LibraryGenerator`，与 u1-l5/u6-l4 讲的 JIT 链路是同一套，只是显式传入 `target="pto"`、`platform="A5"`：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:200-216](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L200-L216)：

```python
prim_func = KERNELS[args.kernel]()
artifact = tilelang.lower(prim_func, target="pto", platform="A5")
print(f"  Source: {len(artifact.kernel_source.splitlines())} lines")

libgen = LibraryGenerator(target="pto", platform="A5")
libgen.update_lib_code(artifact.kernel_source)
libgen.compile_lib()                       # ← 此处进入 4.1 的换库分支
so = libgen.get_lib_path()

kl = ctypes.CDLL(so)
kl.call.argtypes = [ctypes.c_void_p] * 4    # 3 个 buffer + stream
kl.call.restype = None
```

`kl.call` 的参数个数 = kernel 的 buffer 数 + 1（stream），这里 gemm 有 3 个 buffer 所以是 `* 4`。

**启动与验证**。注意 H2D/D2H 用 `rtMemcpy`、启动用 `kl.call(..., stream)`、同步用 `rtStreamSynchronize`——这套「分配→拷入→启动→同步→拷出」正是真实 NPU 异步执行模型的镜像：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:232-256](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L232-L256)：

```python
d_A = dev_malloc(rt, M * K * 2)             # float16 → 每元素 2 字节
d_B = dev_malloc(rt, K * N * 2)
d_C = dev_malloc(rt, M * N * 2)
rt.rtMemcpy(d_A, M * K * 2, h_A.ctypes.data, M * K * 2, 1)   # H2D
rt.rtMemcpy(d_B, K * N * 2, h_B.ctypes.data, K * N * 2, 1)
stream = ctypes.c_void_p()
rt.rtStreamCreate(ctypes.byref(stream), 0)

kl.call(d_A, d_B, d_C, stream)              # 启动 kernel
rt.rtStreamSynchronize(stream)              # 等仿真跑完
rt.rtMemcpy(h_C.ctypes.data, M * N * 2, d_C, M * N * 2, 2)   # D2H

diff = np.abs(h_C.astype(np.float32) - h_Ref)
rel = diff / np.maximum(np.abs(h_Ref), 1e-6)
...
if rel.max() < 0.1:
    print("KERNEL OUTPUT MATCH!")
```

**内置 kernel 的一个关键细节**：样板里的 gemm 把累加器声明成 `float`（而非 `float16`）：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:156-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L156-L166)：

```python
A_L1 = T.alloc_L1((block_M, K_L1), "float16")
B_L1 = T.alloc_L1((K_L1, block_N), "float16")
C_L0 = T.alloc_L0C((block_M, block_N), "float")   # ← A5 pto-isa 要求 float32 累加器
```

这不是偶然——A5 的 pto-isa 要求 L0C 累加器必须是 float32，这是把任意脚本转成仿真版时必须改的一处（见 4.4.1）。

#### 4.3.4 代码实践

**实践目标**：跑通内置 gemm 仿真，并拿到一份 camodel 的指令 trace。

**操作步骤**：

1. 确认 CANN 版本 ≥ 9.0.0，且 `tools/simulator/Ascend950PR_9599` 存在。
2. 在仓库根目录执行：
   ```bash
   python .agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py --log-dir ./my_sim_logs
   ```
3. 观察终端是否依次打印 `=== Load camodel runtime ===` … `=== Launch kernel ===` … `KERNEL OUTPUT MATCH!` `Done!`。
4. 进入 `./my_sim_logs`（即 `CAMODEL_LOG_PATH`），查找 `core*.instr_log.dump`（指令日志）、`core*.ub.rd_log.dump` / `core*.ub.wr_log.dump`（UB 读写 dump）。

**需要观察的现象**：终端最后打印 `KERNEL OUTPUT MATCH!`；`my_sim_logs` 下出现每个 AIC core 的指令 trace 文件。

**预期结果**：仿真正确性验证通过，并能从 dump 文件里看到 kernel 实际下发的指令序列。**待本地验证**（取决于本机是否装了仿真镜像；若未装会在 `_find_sim_lib` 处抛 `Simulator not found`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `kl.call.argtypes = [ctypes.c_void_p] * 4`，而 `dev_malloc` 出来的 `d_A` 直接就能传进去？

> **答案**：`d_A` 是 `ctypes.c_void_p`（设备内存的句柄/地址），`kl.call` 期望的就是一连串设备指针 + 一个 stream 指针。这与 u6-l4 讲过的 host 侧 `call` 符号签名一致——它把所有 buffer 指针打包传给设备函数。`* 4` 对应「3 个 buffer + 1 个 stream」。

**练习 2**：脚本末尾为什么要调 `rtStreamSynchronize` 才能做 D2H 拷贝？

> **答案**：NPU 执行是异步的——`kl.call` 只是「提交到流上」就返回，kernel 可能还没跑完。必须先 `rtStreamSynchronize` 等流上所有任务完成，`d_C` 里才是有效结果，这时的 D2H 拷贝才有意义。这与 CUDA 的 `cudaStreamSynchronize` 语义完全对应。

---

### 4.4 把已有脚本转成仿真版（/tilelang-a5-sim-convert）与仿真局限

#### 4.4.1 概念说明

4.3 的样板只内置了 gemm。如果你手上是别的算子（比如 `examples/gemm/example_gemm.py` 那种用 `torch` + `@tilelang.jit` 写的脚本），要让它跑在仿真器上，需要做一组固定的「翻译」——这就是 `/tilelang-a5-sim-convert` skill 做的事：输入一个脚本路径，输出一个 `*_sim.py`（绝不覆盖原文件）。

翻译的核心是把「依赖真实硬件/torch 的写法」换成「纯 numpy + RT API 的写法」，官方文档把改动总结成一张表（[docs/TileLang-Ascend Programming Guide.md:244-252](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L244-L252)）：

| 原始脚本 | 仿真脚本 |
|---------|---------|
| `import torch` | 删除（用 `numpy` 替代） |
| `torch.randn(...).half().npu()` | `np.zeros(...)` + `rtMalloc` + `rtMemcpy` |
| `@tilelang.jit(out_idx=[-1])` | `def make_kernel(): return main`（DSL 逻辑不变） |
| `T.Tensor((M, K), dtype)` | `T.Tensor((1024, 256), "float16")`（替换为具体数值） |
| `T.alloc_L0C(..., "float16")` | `T.alloc_L0C(..., "float")`（A5 pto-isa 要求 float32 累加器） |
| `c = func(a, b)` | `kl.call(d_A, d_B, d_C, stream)` |
| `torch.testing.assert_close` | `np.abs(got - ref).max()` |

skill 的工作流分三步（[.agents/skills/tilelang-a5-sim-convert/SKILL.md:22-44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/SKILL.md#L22-L44)）：先用 `parse_example.py` 解析原脚本拿到 kernel 的 buffer shape/dtype，再读取模板与原脚本，最后只改「两处」生成 `*_sim.py`。

#### 4.4.2 核心流程

`parse_example.py` 的解析思路很巧妙——它**真的去 import 一遍原脚本**，但把 `torch.npu` mock 掉，从而在不依赖硬件的情况下拿到 kernel 函数与 buffer 信息：

```
parse_example.py <script>
  ├─ mock torch.Tensor.npu 为 no-op（让 .npu() 不报错）
  ├─ 保护 sys.argv 后 exec_module（执行原脚本）
  ├─ 在模块里找 kernel：先 make_kernel()，再 matmul/kernel/main，
  │   再回退扫描所有带 __wrapped__ 的属性
  ├─ 从 prim_func.buffer_map 提取每个参数的 shape/dtype
  └─ 输出 JSON：{ kernel_name, buffers:[{shape,dtype}], num_buffers }
```

输出示例（[parse_example.py:97-114](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/parse_example.py#L97-L114)）：

```python
result = {
    "script_path": script_path,
    "kernel_name": kernel_name,
    "buffers": buffers,            # [{"shape":[1024,1024],"dtype":"float16"}, ...]
    "num_buffers": len(buffers),
}
```

这份 JSON 决定了生成脚本里 `T.Tensor((1024,1024),"float16")` 用什么具体数值、`kl.call.argtypes = [c_void_p] * (num_buffers + 1)` 用几个参数。

#### 4.4.3 源码精读

SKILL.md 把模板拆成几段，标注「哪段不动、哪段要改」（[.agents/skills/tilelang-a5-sim-convert/SKILL.md:10-20](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/SKILL.md#L10-L20)）：

```
行 1-24    import 语句            ← 不动
行 25-96   环境自动设置            ← 不动（_find_ascend_home, _source_cann, _find_sim_lib, setup）
行 99-133  加载 camodel 运行时    ← 不动（load_runtime, dev_malloc）
行 136-166 kernel 定义            ← ★ 第 1 处要改
行 169-260 main() 编译+运行+验证   ← 部分要改
```

也就是说，260 行里**只有两处**需要按目标算子定制：kernel 定义段、main 里的数据准备与 `call` 签名段。其余「环境 → 换库 → 编译 → RT API」全部复用。

`parse_example.py` 找 kernel 的回退逻辑值得一看——它先认 `make_kernel`，再认 `matmul`/`kernel`/`main` 这些常见名，最后兜底扫描所有 `__wrapped__`（即被 `@tilelang.jit` 装饰的函数），并对不同参数个数给不同的默认实参（[parse_example.py:55-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/parse_example.py#L55-L66)）：

```python
n_params = len(params)
if n_params >= 6:
    prim_func = obj.__wrapped__(1024, 512, 256, 128, 256, 64)   # 典型 GEMM 6 参
elif n_params >= 3:
    prim_func = obj.__wrapped__(1024, 512, 256)
else:
    prim_func = obj.__wrapped__()
```

> 注意 `example_gemm.py` 的 `matmul` 正好是 6 个位置参数 `(M, N, K, block_M, block_N, K_L1)`（见 [examples/gemm/example_gemm.py:21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L21)），所以会命中 `>= 6` 分支，被解析成 `1024×512×256 / 128×256×64` 的具体 kernel。这也解释了为什么转换出的 `*_sim.py` 里 `T.Tensor` 的维度是这些具体数值。

#### 4.4.4 代码实践

**实践目标**：把 `examples/gemm/example_gemm.py` 转成仿真脚本并跑通，查看 camodel 指令 trace。

**操作步骤**：

1. 触发 skill（在支持该 skill 的环境里）：
   ```
   /tilelang-a5-sim-convert examples/gemm/example_gemm.py
   ```
   它会生成 `examples/gemm/example_gemm_sim.py`（原文件不动）。
2. 打开生成的 `_sim.py`，对照 4.4.1 的表格，逐项确认改动：`import torch` 是否被删、`T.Tensor` 维度是否变成具体数值、`T.alloc_L0C` 是否改成 `"float"`、`kl.call.argtypes` 是否是 `[c_void_p] * 4`。
3. 运行：
   ```bash
   python examples/gemm/example_gemm_sim.py --log-dir ./gemm_sim_logs
   ```
4. 在 `./gemm_sim_logs` 里查看 `core*.instr_log.dump`，定位其中 `mma`/`copy` 类指令，理解 kernel 实际下发了哪些指令。

**需要观察的现象**：终端打印 `KERNEL OUTPUT MATCH!`；日志目录里能看到每个 core 的指令 trace 文件，里面是 pto 指令（如 `TMATMUL`/`TASSIGN` 等见 u6-l3）。

**预期结果**：转换后的脚本无需真实 NPU 即可在 camodel 上验证正确性，并能从 trace 里读到指令级执行细节。**待本地验证**（需 CANN 9.0.0+ 含 `Ascend950PR_9599` 镜像）。

#### 4.4.5 小练习与答案

**练习 1**：转换时为什么必须把 `T.alloc_L0C(..., "float16")` 改成 `"float"`？不改会怎样？

> **答案**：A5 的 pto-isa 规定 L0C（Cube 累加器）必须是 float32。若保持 `float16`，pto codegen 生成的指令与 A5 仿真器对 L0C 的类型约定不匹配，编译或仿真阶段会出错。这也是 SKILL.md 改动清单里明确列出的一条（[SKILL.md:54](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/SKILL.md#L54)）。注意这和真实 ascendc 路线无关——只针对 pto + A5。

**练习 2**（仿真局限题）：同事想在 camodel 上「顺便测一下 GEMM 的 TFLOPS」，可行吗？为什么？

> **答案**：不可行。camodel 虽是「节拍精确」（能模拟时序），但执行速度比真实硬件慢约 1000 倍以上，且其时序模型与真实硅片仍有差异，**仅适用于功能验证，不适用于性能测试**。性能数据应当用 u7-l4 讲的 `msprof op` 上板采集，或至少用 `msprof op simulator`（那是另一套面向性能的仿真，与 camodel 不是一回事）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**从 example 到仿真验证 + trace 分析**」的完整闭环：

1. **选一个非 gemm 的 example**，比如 `examples/elementwise/elementwise_add.py`（或任一你能找到的、依赖 `torch` 的脚本）。
2. **手工转换**（不依赖 skill，对照 4.4.1 表格自己做一遍）：
   - 删 `import torch`，改用 numpy；
   - 把 `@tilelang.jit` 的工厂函数改成 `def make_kernel(): return main`；
   - 把符号维度替换成具体数值；
   - 数据用 `np.zeros` + `rtMalloc` + `rtMemcpy` 准备；
   - 用 `kl.call(d_in, d_out, stream)` 启动，`rtStreamSynchronize` 后 D2H；
   - 用 numpy 算参考结果并比对。
3. **运行并指定日志目录**，确认 `KERNEL OUTPUT MATCH!`。
4. **打开 `CAMODEL_LOG_PATH` 下的 `core*.instr_log.dump`**，找到你的 elementwise 算子对应的向量指令（如 `TADD` 类），验证你对「代码生成 → 指令」的理解（结合 u6-l2/u6-l3）。
5. **对照检查**：把 `TL_RUN_MODE` 临时去掉再跑一次，观察它在哪里失败（预期是在加载真实 `libruntime` / 初始化真实设备时报错），从而体会 4.1「换库」的必要性。

> 如果本机没有仿真镜像，第 2-4 步无法实跑，请至少完成「手工写出 `_sim.py`」并对照 4.4.1 的表格自查每一处改动，把不确定的现象标注为「待本地验证」。

## 6. 本讲小结

- **camodel 仿真的本质是「换库」**：同一份 kernel `.so`，在编译期把 `-lruntime` 替换成 `-lruntime_camodel`，运行期让动态链接器优先解析到仿真库，从而用 CPU 模拟 NPU；改动集中在 [libgen.py:230-254](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L230-L254)。
- **两个保险缺一不可**：既要改 `-l` 名字（决定找谁），又要把仿真路径排在 `lib64` 前 + 加 `--disable-new-dtags`（让 rpath 传递，解析仿真库的二级依赖）。
- **`TL_RUN_MODE=sim` 是总开关**，`TL_PLATFORM=A5` 只在 sim 时生效；样板脚本用 `os.execve` 自重执行来解决「Python 内改 `LD_LIBRARY_PATH` 对 dlopen 不可靠」的经典坑。
- **仿真没有 `torch.npu`**，数据管理退回到 RT API（`rtMalloc`/`rtMemcpy`/`rtStreamCreate`），`run_a5_sim_template.py` 把这套「环境→换库→编译→RT 调用→验证」打包成一条命令。
- **把任意脚本转仿真版**有固定套路（删 torch、具体化维度、L0C 改 float32、call 替换 launch），`/tilelang-a5-sim-convert` skill + `parse_example.py` 自动完成，260 行模板只需改两处。
- **边界清晰**：仅支持 PTO 后端 + A5、慢约 1000 倍只做功能验证、需 CANN 9.0.0+ 含 `Ascend950PR_9599` 镜像（[Programming Guide 2.4.6](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L268-L273)）。

## 7. 下一步学习建议

- **回到性能世界**：camodel 只解决「对不对」。要解决「快不快」，请回到 u7-l4 的 `msprof op`（上板）与 `msprof op simulator`（性能仿真），注意后者与 camodel 是两套不同的仿真器，不要混淆。
- **深入 codegen 与模板库**：仿真时 dump 出的 `TMATMUL`/`TADD` 等指令来自哪里？答案是 u6-l2（双 codegen）与 u6-l3（pto-isa 模板库）。结合 trace 阅读这两个讲义，能把「TIR intrinsic → C++ 指令宏」彻底打通。
- **写一个能被 autotuner 跑的算子**：仿真是 autotuner（u7-l6）在没有硬件时的「正确性 oracle」——你可以先用 camodel 验证候选配置的正确性，再到真机上比性能。尝试为本讲的 gemm 定义一个 `block_M/block_N/K_L1` 调参空间，用 camodel 做正确性筛选。
- **阅读样板脚本全文**：本讲只精读了关键片段，建议完整通读 [run_a5_sim_template.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py) 全 260 行，它是「ctypes 驱动 CANN 运行时」的一份极好范例，对理解 u6-l4 的运行时加载也有帮助。
```
