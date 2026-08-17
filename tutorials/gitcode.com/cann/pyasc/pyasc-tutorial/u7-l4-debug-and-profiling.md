# 调试与调优：dump、printf 与 msprof

## 1. 本讲目标

写出一个「结果正确」的算子只完成了任务的一半；当结果错误、或者性能不达标时，你需要一套工具来回答三个问题：

1. 编译器到底把我的 Python 代码变成了什么？（中间产物级调试）
2. 算子在设备上运行时，数据到底是什么值？（运行时功能调试）
3. 时间花在了哪条流水线上？（性能调优）

学完本讲，你应该能够：

- 掌握 `PYASC_DUMP_PATH` 四级中间产物（codegen.mlir → ascir.mlir → ascendc.cpp → binary.o）的导出时机与阅读方法。
- 说清 `asc.printf` / `asc.dump_tensor` 从 Python 前端到设备侧输出的一条完整链路：`DetectEnableDebugPass` 打属性 → `ASCENDC_DUMP` 宏 → `InitDump` 注入 → 75 MiB dump 缓冲 → `PrintWorkSpace` 回收打印。
- 理解 `CompileOptions` 中 `debug`、`verify_sync`、`print_ir_before_all`、`strip_loc` 四个调试相关选项各自拨动的是哪个开关。
- 理解 `MsprofLauncher` 与 `npu_utils.cpp` 如何在 launch 前后打 msprof 打点，以及 `msprof op` / `msprof op simulator` 两种采集方式的输出结构。
- 能独立完成一份「IR → C 代码 → profiling 数据」三段式调优报告。

## 2. 前置知识

本讲默认你已完成 u1-l4（跑通 Add 示例）和 u3-l4（CompileOptions 与 Pass 流水线），这里把要用到的旧知识快速对齐，并补充两个新概念。

**已学知识的回顾：**

- **JIT 主链路**：`JITFunction._run` 走「选项分流 → 查缓存 → AST→ASC-IR → 跑 Pass → 翻译成 Ascend C → 毕昇编译 → Launcher 下发」。本讲的所有调试手段都是挂在这条链路的某个环节上的「观察窗口」。
- **CompileOptions**：从小括号或装饰器进入、参与文件缓存 key 的编译选项袋。本讲的 `debug`、`verify_sync`、`print_ir_before_all` 都是它的字段——所以**打开调试选项会产生新的缓存条目**，不会污染正常编译的缓存。
- **Detect 系列 Pass 与 IR 属性回传**：后端 Pass 可以在模块上打 `asc.xxx` 属性，Python 侧随后读取。本讲的 `asc.enable_debug` 正是这条通道之一。

**新概念一：条件编译宏 `ASCENDC_DUMP`。**
最终生成的 Ascend C 代码里，设备侧打印相关代码被 `#if defined ASCENDC_DUMP` 包裹；毕昇编译命令行上的 `-DASCENDC_DUMP=0/1` 决定这些代码是否真正编进二进制。也就是说，「打印代码存在」和「打印代码生效」是两件事，前者由 Pass 检测决定，后者由宏决定。

**新概念二：msprof 打点的 L0/L1 开关。**
昇腾的 profiling 工具 msprof 通过回调通知被测进程「本次采集开启了哪个级别」。pyasc 在 `npu_utils.cpp` 里注册了这个回调：L0 对应任务时间采集（上板默认），L1 对应更细的算子级信息。**没有开 msprof 时打点函数直接空转返回**，因此给正常执行带来的开销接近零。

**一个容易混淆的点**：本讲出现三个「dump」，含义各不相同——

| 名字 | 是什么 | 由谁控制 |
|------|--------|----------|
| `PYASC_DUMP_PATH` | 把编译中间产物写到磁盘目录 | 环境变量（编译期） |
| `ASCENDC_DUMP`（环境变量） | 总开关：设备侧打印链路是否启用 | 环境变量，默认视为 true |
| `ASCENDC_DUMP`（C 宏） | 决定打印代码是否编进二进制 | `_get_compiler_cmd` 根据 enable_debug 注入 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `docs/op_debug_prof.md` | 官方调试调优指南：printf/dump_tensor 用法、msprof op 上板与仿真命令 |
| `python/asc/runtime/compiler.py` | CompileOptions 定义、四级 dump 落盘、enable_debug 判定、InitDump 注入、毕昇命令行组装 |
| `python/asc/runtime/utils.py` | `ONE_CORE_DUMP_SIZE`/`TOTAL_DUMP_SIZE` 常量与 `FileUtils.dump_file` 落盘工具 |
| `python/asc/language/basic/dump_tensor.py` | `asc.printf`/`asc.dump_tensor` 的 Python 前端（创建 IR Op） |
| `lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp` | 检测 PrintfOp/DumpTensorOp 并打 `asc.enable_debug` 属性 |
| `python/asc/runtime/launcher.py` | MsprofLauncher 打点、debug 缓冲注入与打印回收 |
| `python/asc/lib/runtime/print_utils.py` / `print_utils.cpp` | 在线编译并加载 `PrintWorkSpace`（解析设备侧 dump 工作区） |
| `python/asc/lib/runtime/npu_utils.cpp` | msprof 回调注册、L0/L1 开关、打点上报 C 实现 |
| `python/asc/lib/runtime/interface.py` | `msprof_task_type`：CoreType → msprof 任务类型映射 |
| `examples/08_rmsnorm/profile_msprof.py` | msprof op 批量采集 + CSV 解析的完整工程范例 |
| `examples/08_rmsnorm/bench_rmsnorm.py` | 被 profile 的被测脚本（三种后端） |
| `examples/01_add/add.py` | 综合实践的改造对象 |

## 4. 核心概念与源码讲解

### 4.1 dump 链路：四级中间产物是怎么落盘的

#### 4.1.1 概念说明

JIT 编译是一条多级变换的流水线：Python AST → ASC-IR（Pass 前）→ ASC-IR（Pass 后）→ Ascend C 源码 → ELF 二进制。任何一级出错，最终表现都只是「结果错」或「编译失败」，不看中间产物就无从下手。

pyasc 的做法是：设置环境变量 `PYASC_DUMP_PATH=<目录>` 后，`Compiler` 在链路的四个关键点把当时的产物写进该目录，文件名固定：

| 文件 | 产生时机 | 内容 |
|------|----------|------|
| `codegen.mlir` | Pass 流水线**运行之前** | 前端 FunctionVisitor 直接生成的 IR |
| `ascir.mlir` | Pass 流水线**运行之后** | 优化/改写后的最终 IR |
| `ascendc.cpp` | 翻译成 Ascend C 之后（含 debug 注入） | 发射层产出的 C++ 源码 |
| `binary.o` | 毕昇编译链接之后 | 可下发设备的 ELF 镜像 |

这个设计的好处是「对照阅读」：同一个算子在 codegen.mlir 和 ascir.mlir 之间的差异就是全部 Pass 做的事；ascir.mlir 里的每个 Op 都能在 ascendc.cpp 里找到对应的 C 调用。

#### 4.1.2 核心流程

```text
Compiler.__init__
  └─ 读 PYASC_DUMP_PATH 环境变量 → self.dump_dir（Path），并确保目录存在
Compiler.run(mod, func_name)
  ├─ dump_file(dump_dir, "codegen.mlir", str(mod))   # Pass 前
  ├─ run_passes(mod)                                  # 16 个 Pass
  ├─ dump_file(dump_dir, "ascir.mlir", str(mod))      # Pass 后
  ├─ source = run_translation(mod)                    # IR → Ascend C
  ├─ 若 enable_debug：source = _gen_init_dump_code(...)  # 注入 InitDump
  ├─ dump_file(dump_dir, "ascendc.cpp", source)
  └─ run_compilation(source, kernel_args)
       └─ 编译链接后：shutil.copyfile(dst, dump_dir / "binary.o")
```

注意 dump 只发生在**真实编译**时——若两级缓存命中，`Compiler.run` 根本不会被调用，dump 目录不会更新。想强制刷新可用 `always_compile=True`（见 u3-l8）。

#### 4.1.3 源码精读

dump 目录在构造 `Compiler` 时一次性确定：

- [python/asc/runtime/compiler.py:98-104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L98-L104)：读取环境变量 `PYASC_DUMP_PATH` 并 resolve 成绝对路径存入 `self.dump_dir`，随后 `FileUtils.create_dir` 逐级建目录（权限 0750）。目录非法时直接抛 `RuntimeError`。

四级落盘点集中在 `Compiler.run`（`@final`，不可覆写）：

- [python/asc/runtime/compiler.py:162-173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173)：第一行就把**未跑 Pass 的 IR** 写为 `codegen.mlir`；`run_passes` 之后写 `ascir.mlir`；`run_translation` 得到 Ascend C 字符串，若 `enable_debug` 则先注入 InitDump 代码（4.2 节详述），再写 `ascendc.cpp`——所以 dump 出的 ascendc.cpp **已经包含**注入后的调试代码，可直接对照。

- [python/asc/runtime/compiler.py:199-200](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L199-L200)：`run_compilation` 在毕昇编译与链接完成后，把临时目录里的 `output.o` 拷贝为 dump 目录下的 `binary.o`——这是与真实下发字节完全相同的产物，可用 `readelf` 等工具离线检查。

落盘动作本身是 `FileUtils.dump_file`：

- [python/asc/runtime/utils.py:22-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L22-L35)：`dump_dir` 为 None 时（未设置环境变量）直接返回，一行不写——这就是「不开 dump 零开销」的由来。data 既可传字符串/字节，也可传一个「惰性回调」，真正需要写盘时才求值。

同一文件顶部还定义了 dump 缓冲的尺寸常量：

- [python/asc/runtime/utils.py:13-14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L13-L14)：`ONE_CORE_DUMP_SIZE = 1048576`（每核 1 MiB），`TOTAL_DUMP_SIZE = ONE_CORE_DUMP_SIZE * 75`（总共 75 MiB，即按 75 核上限预留）。这两个数字稍后在 enable_debug 链路中会再次出现。

#### 4.1.4 代码实践

1. **实践目标**：亲手导出并对照阅读一份四级中间产物。
2. **操作步骤**：
   ```bash
   cd examples/01_add
   mkdir -p /tmp/add_dump
   PYASC_DUMP_PATH=/tmp/add_dump python3 add.py -r Model -v Ascend910B1
   ls -la /tmp/add_dump
   ```
3. **需要观察的现象**：目录下出现 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`、`binary.o` 四个文件；`ascendc.cpp` 里能看到 `AscendC::Add` 等 C 调用，`codegen.mlir` 与 `ascir.mlir` 的差异包含张量物化（u6-l2 讲过的 `local_tensor_auto` 改写）。
4. **预期结果**：在 `ascir.mlir` 中找到 `ascendc.Add` 一类的 Op，再在 `ascendc.cpp` 中找到对应 C++ 语句，确认「IR 名 → C 调用」的映射（可复习 u5-l1 的四名合一反查法）。文件大小上 `ascir.mlir` 通常比 `codegen.mlir` 结构更规整（样板已生成）。
5. 结果细节（如具体 Op 行号）依赖本地编译，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么改了 kernel 源码后连续两次运行，第二次的 dump 目录没有更新？
**答案**：dump 只在真实编译时发生。源码哈希变了会使文件缓存失效、触发重编并刷新 dump；但若源码未变而只是重复运行，两级缓存命中，`Compiler.run` 不被调用，dump 目录保持旧内容。可用 `always_compile=True` 强制每次重编刷新。

**练习 2**：`binary.o` 和 ascendc.cpp 是什么关系？
**答案**：ascendc.cpp 是发射层生成的 Ascend C 源码；pyasc 把它写入临时文件 `input.cce`，交毕昇编译器（`bisheng -c -x cce ...`）编成 `.o` 再由 `ld.lld -static` 链接成可执行 ELF；`binary.o` 就是这个最终 ELF 的逐字节拷贝，是 Launcher 真正注册下发的二进制。

**练习 3**：`FileUtils.dump_file` 为什么支持传入回调而不是只支持字符串？
**答案**：回调把「生成产物文本」的成本推迟到确认需要写盘之后——当 `dump_dir` 为 None 时直接 return，连序列化 IR/源码的字符串构造都不发生，保证未开启 dump 时零开销。

### 4.2 enable_debug 注入：从 PrintfOp 到 InitDump 代码

#### 4.2.1 概念说明

设备侧（NPU/仿真器上的核函数里）没有 stdout，`asc.printf`/`asc.dump_tensor` 的输出走的是一条专门的**dump 工作区**机制：核函数把打印内容写进一块设备内存，执行结束后 Host 侧把它拷回来解析成文本。

启用这条链路需要**四个条件同时成立**，理解这个「与」逻辑是本模块的核心：

1. kernel 源码里确实用了 `asc.printf` 或 `asc.dump_tensor`（后端 Pass 检测后给模块打 `asc.enable_debug` 属性）；
2. 环境变量 `ASCENDC_DUMP` 不为 `false`（默认视为 true，即默认放行）；
3. 编译命令行注入 `-DASCENDC_DUMP=1` 宏，设备侧打印代码才真正编进二进制；
4. Launcher 在参数表尾部追加 75 MiB dump 缓冲，并在执行后调用打印接口回收。

而 `CompileOptions.debug` 是另一个维度的开关：它给毕昇编译命令加 `-g` 等调试信息选项，与 enable_debug 链路互相独立。

#### 4.2.2 核心流程

```text
Python 源码含 asc.printf/asc.dump_tensor
  ↓ FunctionVisitor（asc.PrintfOp / asc.DumpTensorOp 进入 IR）
DetectEnableDebugPass（postprocessing 阶段）
  ↓ 模块打 UnitAttr "asc.enable_debug"
Compiler.run_passes 末尾：
  enable_debug = 有属性 且 ASCENDC_DUMP 环境变量不为 "false"
  ↓
Compiler.run：if enable_debug → _gen_init_dump_code(source)
  · kernel 签名追加参数 ", __gm__ uint8_t* dump_addr"
  · 函数体开头插入（#if defined ASCENDC_DUMP 包裹）：
      AscendC::InitDump(is_mix, dump_addr, 1MiB)
      GetCannVersion + AscendC::printf 版本信息
  ↓
_get_compiler_cmd：-DASCENDC_DUMP=1（否则 0）
  ↓
毕昇编译出带打印能力的 binary.o
```

#### 4.2.3 源码精读

先看用户直接接触的两个前端接口（它们都只是「向 IR 追加一个 Op」）：

- [python/asc/language/basic/dump_tensor.py:44-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L44-L63)：`asc.printf(desc, *params)`。实现里有个值得注意的细节——它把描述串按 `%s` 切开，遇到 Python `str` 参数就**在编译期直接拼进描述串**（`new_desc`），只有 `IRValue`（设备侧值）才作为可变参数传给 `create_asc_PrintfOp`。字符串拼接过、设备值走参数，这就是文档要求 `\n` 写成 `\\n` 的根源：描述串会原样进入生成的 C 代码。

- [python/asc/language/basic/dump_tensor.py:29-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L29-L41)：`asc.dump_tensor(tensor, desc, dump_size, shape_info)`。`desc` 是给这次 dump 编的编号（输出里能看到 `desc=0/1`），`dump_size` 是打印的元素个数，可选 `shape_info` 让输出按二维排版。标量经 `_mat`（materialize_ir_value）物化为 uint32 后调用 `create_asc_DumpTensorOp`。

后端检测 Pass 非常短，直接全文读懂：

- [lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp:31-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp#L31-L42)：`runOnOperation` 用 `op.walk` 在整个模块里找 `PrintfOp` 和 `DumpTensorOp`，找到任意一个就给模块设置 `UnitAttr`（`attr::enable_debug`，即 `asc.enable_debug`）。walk 配 `WalkResult::interrupt()` 找到第一个就停，不做多余遍历。这正是 u3-l4/u6-l4 讲过的「后端写 IR 属性、Python 读取」的回传通道。

Python 侧的读取与「与」逻辑：

- [python/asc/runtime/compiler.py:190-191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L190-L191)：`run_passes` 末尾计算 `self.enable_debug = 模块有 asc.enable_debug 属性 and ASCENDC_DUMP 环境变量（缺省 "True"）小写不等于 "false"`。也就是说想**临时关闭**设备侧打印而保留源码，只需 `export ASCENDC_DUMP=false`，不必删代码。

注入口是本模块最「黑科技」的一段——直接对生成的 C 源码做文本手术：

- [python/asc/runtime/compiler.py:237-272](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L237-L272)：`_gen_init_dump_code`。逐行看关键点：
  - 构造 `dump_code` 字符串：外层包 `#if defined ASCENDC_DUMP`，声明 `ascendc_one_core_dump_size = 1048576`（即 4.1 节的 `ONE_CORE_DUMP_SIZE`）；MIX 类核类型调 `AscendC::InitDump(true, dump_addr, ...)`，其余调 `InitDump(false, ...)`；
  - 随后追加 `GetCannVersion` + `AscendC::printf` 打印 CANN 版本与时间戳——这解释了官方文档输出样例里第一行之后总有 `CANN Version: XX.XX, TimeStamp: ...`；
  - 最后逐行扫描源码：找到同时含 `func_name` 和 `__aicore__` 的那一行（即 kernel 入口声明），在形参列表末尾插入 `, __gm__ uint8_t* dump_addr)`，并把 `dump_code` 插到该行之后——于是 dump 工作区地址作为一个**额外的 kernel 参数**从 Host 传进来（与 u3-l6 讲的 ffts_addr 隐藏参数同一手法，但这个是在 C 源码文本层注入的）。

宏开关与调试选项落在毕昇命令行上：

- [python/asc/runtime/compiler.py:331-336](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L331-L336)：`enable_debug` 为真加 `-DASCENDC_DUMP=1`，否则 `-DASCENDC_DUMP=0`（`#if defined` 判断的是宏是否**定义为真值**）；独立的 `options.debug` 为真时加 `-g` 和 `-mllvm --cce-aicore-jump-expand=true`，为调试器/仿真提供更友好的指令序列。

CompileOptions 中其余调试相关字段的落点（都在 Pass 调度处）：

- [python/asc/runtime/compiler.py:178-179](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L178-L179)：`print_ir_before_all=True` 时 `pm.enable_printing()`，每个 Pass 运行前把 IR 全文打到 stdout——比「只看首尾两级 dump」细得多，是定位「哪个 Pass 改坏了 IR」的利器。
- [python/asc/runtime/compiler.py:227-230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L227-L230)：`verify_sync=True` 在 postprocessing 末尾追加 VerifySync（u6-l3：按队列记账检查 alloc/free、enque/deque 配对，只报 warning 不拦截）；`strip_loc=True` 追加 strip_debug_info，去掉 IR 中的源码位置信息（发布场景常用）。
- [python/asc/runtime/compiler.py:29-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L29-L32)：以上四个字段在 CompileOptions 中的声明位置。再次提醒：它们都进文件缓存 key（u3-l8），开关一变就是新缓存条目。

#### 4.2.4 代码实践

1. **实践目标**：完整观察「源码里有没有 printf」对生成产物的影响。
2. **操作步骤**：
   - 复制 `examples/01_add/add.py` 为 `add_dbg.py`（不要改原文件）；
   - 在 [examples/01_add/add.py:49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49) 的 for 循环前加一行 `asc.printf("before loop\\n")`（示例代码，注意双反斜杠转义）；
   - `PYASC_DUMP_PATH=/tmp/d1 python3 add.py -r Model -v Ascend910B1`（未加 printf 的原版），再 `PYASC_DUMP_PATH=/tmp/d2 python3 add_dbg.py -r Model -v Ascend910B1`；
   - `grep -n "InitDump\|dump_addr\|ASCENDC_DUMP" /tmp/d2/ascendc.cpp`，并与 `/tmp/d1/ascendc.cpp` 对比。
3. **需要观察的现象**：只有 d2 的 ascendc.cpp 出现 `dump_addr` 形参、`AscendC::InitDump` 和 `#if defined ASCENDC_DUMP` 块；d1 的 ascendc.cpp 没有这些注入。运行时 d2 的 stdout 会在结果输出前多出 DumpHead 头与 `before loop`（每核一份，见 docs 样例格式）。
4. **预期结果**：验证了 4.2.1 的条件链——`printf` 的存在 → Pass 打属性 → enable_debug 为真 → C 源码注入 + 宏置 1。再执行 `ASCENDC_DUMP=false PYASC_DUMP_PATH=/tmp/d3 python3 add_dbg.py -r Model -v Ascend910B1`，d3 的 ascendc.cpp 应与 d1 一样无注入。
5. 具体输出文本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ASCENDC_DUMP=false` 能关掉打印，而不用删掉代码里的 `asc.printf`？
**答案**：`run_passes` 末尾判定 `enable_debug = 有属性 and 环境变量不为 "false"`（compiler.py:190-191）。属性仍在 IR 上，但 enable_debug 为假，于是既不注入 InitDump，编译命令也是 `-DASCENDC_DUMP=0`，打印代码不进二进制；Host 侧也不会追加 dump 缓冲。

**练习 2**：`CompileOptions(debug=True)` 和 enable_debug（printf 链路）是什么关系？
**答案**：两者独立。`debug=True` 只影响毕昇命令行（加 `-g` 与 jump-expand，见 compiler.py:335-336），服务于反汇编/单步级调试；enable_debug 由「源码是否用 printf/dump_tensor」驱动，服务设备侧数据打印。可以只开其一，也可以同时开。

**练习 3**：`_gen_init_dump_code` 是怎么把 `dump_addr` 传给核函数的？这和 u3-l6 的 ffts_addr 注入有何异同？
**答案**：它对翻译出的 Ascend C 文本逐行扫描，定位含 `func_name` 与 `__aicore__` 的入口声明行，在形参列表末尾插入 `, __gm__ uint8_t* dump_addr`。相同点：都是「隐藏参数」，都由 Host 在 launch 时补上实参；不同点：ffts_addr 由 LegalizeKernelArgs 在 **IR 层**插桩（u6-l4），dump_addr 由 Python 在 **C 源码文本层**注入。

### 4.3 print_utils：设备侧打印的 Host 侧回收

#### 4.3.1 概念说明

4.2 节解决了「让核函数会打印」，本节解决「打印的内容怎么变成终端上的文本」。设备侧的 `AscendC::printf`/`DumpTensor` 并不直接输出，而是把带格式的内容写进那块 75 MiB 的 dump 工作区；执行结束后由 Host 侧调用 CANN 的 `Adx::AdumpPrintWorkSpace` 解析工作区并打印到进程 stdout。

pyasc 对这个 C 接口做了两层封装：`print_utils.cpp`（22 行的 extern "C" 薄包装）和 `print_utils.py`（在线编译 + 缓存 + ctypes 加载）。这个「源码随包分发、首次使用在线编译、sha256 缓存复用」的模式你在 u3-l7（rt_wrapper/npu_utils）和 u7-l3（lib/host bindings）已经见过两次了——本讲是同一模式的最后一个实例。

#### 4.3.2 核心流程

```text
Launcher.run（enable_debug=True 时）
  ├─ kernel_args.append(np.zeros(TOTAL_DUMP_SIZE, dtype=int8))  # 75 MiB 缓冲成为最后一个指针参数
  ↓ launch_kernel：参数 blob 组包 → rt.launch_kernel 下发 → synchronize
  ↓ 回拷阶段（按 memory_args 逆序判断）：
  │    最后一个 MemoryHandle 不走 copy_from_device，而是
  │    rt.call_print_interface(inputs[-1], TOTAL_DUMP_SIZE, stream, func_name)
  ↓ PrintInterface（首次调用时构造）
  ├─ 缓存 key = sha256(print_utils.cpp 全文 + CANN version.cfg)
  ├─ 未命中 → g++ 在线编译 print_utils.cpp（链 -lascend_dump -lc_sec）→ 存入文件缓存
  └─ ctypes.CDLL 加载 print_interface.so → 调 PrintWorkSpace
  ↓ C++：Adx::AdumpPrintWorkSpace(workSpaceAddr, size, stream, opType) → stdout
```

#### 4.3.3 源码精读

Launcher 侧的两处配合：

- [python/asc/runtime/launcher.py:143-144](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L143-L144)：`run` 里若 `kernel.enable_debug`，在参数表**末尾**追加一个 `np.zeros(TOTAL_DUMP_SIZE, dtype=np.int8)`——75 MiB 的全零数组经 `expand_kernel_args` 变成 `MemoryHandle`，`copy_to_device` 后其设备地址正好填进核函数被注入的 `dump_addr` 形参（参数顺序严格对齐：注入在最后，追加也在最后）。

- [python/asc/runtime/launcher.py:118-125](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L118-L125)：`synchronize` 之后逐个处理设备内存参数。普通参数走 `arg.copy_from_device()` 回拷；**最后一个** memory 参数在 `enable_debug` 时改走 `rt.call_print_interface(inputs[-1], utils.TOTAL_DUMP_SIZE, stream, func_name)`——把工作区设备地址、总大小、stream 和算子名交给打印接口，工作区内容由 CANN 库解析输出，而不是当普通数据拷回。finally 里统一 `release_memory`，所以 dump 缓冲同样只存活这一次 launch。

Python 封装层：

- [python/asc/lib/runtime/print_utils.py:52-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.py#L52-L75)：`PrintInterface.__init__` 读同目录的 `print_utils.cpp` 源文与 CANN `version.cfg`，拼起来取 sha256 作为缓存 key，从 `FileCacheManager` 找 `print_interface.so`；未命中则在临时目录里现场编译（见下）并把产物字节写入缓存。最后 `ctypes.CDLL(rt_lib, RTLD_LOCAL)` 加载。`call` 方法用 `getattr(self.lib, "PrintWorkSpace")` 拿到函数指针直接调用。

- [python/asc/lib/runtime/print_utils.py:24-49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.py#L24-L49)：`build_print_utils` 组装编译命令：从 `CC` 环境变量或 `which c++/g++` 找编译器，`-w` 关警告，关键是 `-L$ASCEND_HOME_PATH/lib64 -lascend_dump -lc_sec`——真正的解析实现 `AdumpPrintWorkSpace` 在 CANN 的 `libascend_dump` 里，pyasc 只提供跳板。`-shared -fPIC` 产出动态库。

- [python/asc/lib/runtime/print_utils.py:86-89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.py#L86-L89)：模块级懒加载单例 `print_interface`；`call_print_interface` 被首次调用时才构造。该函数经 [python/asc/lib/runtime/__init__.py:62](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/__init__.py#L62) 重导出为 `rt.call_print_interface`，即 launcher 调用的名字。

C++ 跳板全文只有一处调用：

- [python/asc/lib/runtime/print_utils.cpp:13-21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.cpp#L13-L21)：声明 `Adx::AdumpPrintWorkSpace(const void* workSpaceAddr, size_t dumpWorkSpaceSize, void* stream, const char* opType)` 并在 `extern "C" PrintWorkSpace` 里原样转发——extern "C" 保证 ctypes 能按名字找到符号（C++ 名字改编被关闭）。文档输出样例首行 `opType=v, DumpHead: AIV-0, CoreType=AIV, ...` 就是这个 opType 参数与工作区头部的解析结果。

输出格式与约束的权威说明见官方文档：

- [docs/op_debug_prof.md:26-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L26-L45)：printf 输出样例（DumpHead 头 + 逐核内容）与两条约束：`\n` 需转义、printf/dump_tensor 有性能影响仅调测期使用。
- [docs/op_debug_prof.md:76-91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L76-L91)：dump_tensor 输出样例：`desc=0, addr=..., position=GM` 与 `desc=1, position=UB` 各一段，GM 侧可借 ShapeInfo 排成二维、越界部分打印 `-`。

#### 4.3.4 代码实践

1. **实践目标**：用 dump_tensor 同时观察 GM 输入与 UB 中的局部数据，并理解打印发生在哪个时机。
2. **操作步骤**：在 4.2.4 的 `add_dbg.py` 基础上：
   - 在 `set_global_buffer` 三行之后加入（示例代码，参照 [docs/op_debug_prof.md:58-60](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L58-L60) 的写法）：
     ```python
     tmp_array = asc.array(asc.float32, [4, 16])
     asc.dump_tensor(x_gm, 0, 64, asc.ShapeInfo(tmp_array))
     ```
   - 在 for 循环体内 `asc.data_copy` 搬入 x 之后加 `if i == 0: asc.dump_tensor(x_local[0:], 1, 32)`（注意 01_add 中 x_local 本身就偏移 0，切片写法复习 u2-l2）；
   - `python3 add_dbg.py -r Model -v Ascend910B1` 运行。
3. **需要观察的现象**：stdout 先出现 DumpHead 头与 CANN Version 行，随后是 `desc=0 ... position=GM` 的 4×16 矩形数据与 `desc=1 ... position=UB` 的一行数据；断言 `torch.allclose` 仍通过。
4. **预期结果**：GM 段数据等于 torch 输入 x 的前 64 个元素；UB 段数据等于第一轮搬入的 tile。同时体会文档约束：dump 语句会让总耗时明显增加。
5. 具体数值**待本地验证**（取决于 torch 随机输入）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dump 缓冲要按「75 MiB = 1 MiB × 75」预留？
**答案**：`ONE_CORE_DUMP_SIZE` 是每核打印工作区上限（注入代码里 `ascendc_one_core_dump_size` 用的同一常量），`TOTAL_DUMP_SIZE` 按最多 75 个核预留总量（utils.py:13-14）。多核各写自己那段，Host 一次回收整块解析。

**练习 2**：`print_interface.so` 第二次运行还会重新编译吗？缓存 key 为什么要把 `version.cfg` 算进去？
**答案**：不会。key = sha256(cpp 源文 + CANN version.cfg)（print_utils.py:58-63），首次编译后产物进 `FileCacheManager`，之后直接命中加载。纳入 version.cfg 是因为实现链在 CANN 的 `libascend_dump` 上，换 CANN 版本后解析格式可能变化，强制重新编译以避免错配——与 u7-l3 lib/host 缓存按 CANN 版本隔离同思路。

**练习 3**：如果 `enable_debug=False`，Launcher 会为 dump 缓冲付出什么成本？
**答案**：零成本。`run` 里的追加（launcher.py:143-144）有 `if kernel.enable_debug` 守门；回拷分支（launcher.py:120-121）同样判断 enable_debug，普通参数照常回拷。缓冲不存在、接口不加载（PrintInterface 是懒加载单例）。

### 4.4 msprof 打点：让算子出现在 profiling 时间线里

#### 4.4.1 概念说明

前两节是「功能调试」；性能调优的主工具是 CANN 的 **msprof op**。它有两种用法（见 [docs/op_debug_prof.md:103-113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L103-L113)）：

- **msprof op（上板）**：`msprof op --output=... python xxx.py -r NPU`，产出 Memory.csv、PipeUtilization.csv 等指标表与 visualize_data.bin（导入 MindStudio Insight 看内存热力图、Roofline、通算流水图）；
- **msprof op simulator（仿真）**：`msprof op simulator --output=... python xxx.py -r Model -v Ascend910B1`，产出每核的指令流水 CSV 与 trace.json。

pyasc 与 msprof 的结合点在 Host 侧：pyasc 是「自己拼参数、自己调 `rt.launch_kernel`」下发任务的，不走框架调度，所以**必须自己向 msprof 上报**「这里有一个名为 X 的算子任务、用了 N 个核、属于什么任务类型」，msprof 的时间线里才会出现这个算子的条目。这就是 `MsprofLauncher` 的职责——launcher 在下发前后各打一个点。

关键设计：打点是否生效由 msprof 侧控制。pyasc 启动时注册回调；msprof 采集开始时通知「开了 L0/L1」，打点函数检查开关，没开就直接返回。因此不开 msprof 跑示例，打点链路是空转的。

#### 4.4.2 核心流程

```text
import npu_utils 扩展时（_lazy_init 链路）
  └─ PyInit_npu_utils：MsprofRegisterCallback(moduleId=8, ProfCtrlHandle)
msprof 启动采集 → 回调 ProfCtrlHandle(PROF_COMMANDHANDLE_TYPE_START, profSwitch)
  ├─ 含 PROF_TASK_TIME      → msprofFlagL0 = 1
  └─ 含 PROF_TASK_TIME_L1   → msprofFlagL1 = 1
msprof 停止 → 回调 STOP → 两个 flag 清零

每次 kernel 下发（Launcher.launch_kernel）：
  msprof.start()                      # 记 start_time = MsprofSysCycleTime()
  rt.launch_kernel(...)               # 真正下发
  msprof.process(name, core_num, task_type)
      ├─ MsprofReportCompactInfo      # 上报算子名/核数/任务类型（L1）
      └─ MsprofReportApi(start,end)   # 上报 API 区间耗时（L0/L1）
Model（仿真）模式下 MsprofLauncher.start/process 直接 return
```

#### 4.4.3 源码精读

Python 侧的打点编排：

- [python/asc/runtime/launcher.py:24-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L24-L45)：`MsprofLauncher`。构造时保存 `is_model`（来自 `rt.is_model()`）和 `rt.npu_utils()` 扩展对象。`start` 取系统周期时间作为区间起点；`process` 先上报 `msprof_report_compact_info`（时间戳、算子名、block 数、任务类型），再用起点/终点调 `msprof_report_api`。两个方法第一行都是 `if self.is_model: return`——**Model 仿真模式下 Host 打点是空操作**（仿真器的流水数据由 simulator 采集，不靠 API 打点）。

- [python/asc/runtime/launcher.py:111-117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L111-L117)：打点在 `launch_kernel` 中的确切位置——`msprof.start()` 紧贴 `rt.launch_kernel` 之前，`msprof.process(func_name, core_num, rt.msprof_task_type(core_type))` 紧随其后。注意传的是**函数名与核数**，这正是 msprof 输出 CSV 里 `Op Name`、`Block Dim` 列的数据来源。

- [python/asc/lib/runtime/interface.py:96-106](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L96-L106)：`msprof_task_type` 把 `CoreType` 映射为 msprof 任务类型枚举值：VectorCore → AIV，AiCore/CubeCore → AI_CORE，其余 → AI_CPU。与 u3-l5 讲过的 `magic_elf_value`（CoreType → ELF 魔数）是同型的「核类型查表」。

C++ 侧的开关与上报：

- [python/asc/lib/runtime/npu_utils.cpp:211-222](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp#L211-L222)：模块初始化 `PyInit_npu_utils` 里 `aclInit` 后立刻 `MsprofRegisterCallback(moduleId, ProfCtrlHandle)`——扩展一加载就把回调挂上，此后 msprof 的启停命令都会送到 `ProfCtrlHandle`。

- [python/asc/lib/runtime/npu_utils.cpp:33-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp#L33-L63)：`ProfCtrlHandle` 解析 msprof 命令：`PROF_COMMANDHANDLE_TYPE_START` 时检查 profSwitch 位，含 `PROF_TASK_TIME` 置 `msprofFlagL0`、含 `PROF_TASK_TIME_L1` 置 `msprofFlagL1`；`STOP` 时双双清零。（`SEPARATE_PKG_ARCH` 分支只是不同 CANN 包架构下的宏名差异，语义相同。）

- [python/asc/lib/runtime/npu_utils.cpp:86-99](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp#L86-L99)：`MsprofSysCycleTime`——两个 flag 都为 0 时直接返回 0，不触碰 profiling API；这就是「不开 msprof 零开销」的实现层证据。

- [python/asc/lib/runtime/npu_utils.cpp:101-168](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp#L101-L168)：`MsprofReportApi` 用 `MsprofGetHashId(算子名)` 生成 itemId，组装 `MsprofApi`（LAUNCH 类型、线程 id、起止时间）上报；`MsprofReportCompactInfo` 组装 `MsprofCompactInfo`（opName/taskType/blockDim 等基本字段）上报。两者的守卫不同：ReportApi 检查 `L0 || L1`，ReportCompactInfo 只检查 `L1`——即 L1 级采集才有算子基本信息的 compact 上报。

工程化范例（08 示例）：

- [examples/08_rmsnorm/profile_msprof.py:77-117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/profile_msprof.py#L77-L117)：`profile_one` 对一种后端 × 一种形状构造并执行 `msprof op --output=... python3 bench_rmsnorm.py --backend pyasc ... -r NPU -v Ascend910B4` 命令（pyasc/ascendc/torch_npu 三种后端三种命令形态），随后解析输出目录。

- [examples/08_rmsnorm/profile_msprof.py:112-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/profile_msprof.py#L112-L136)：结果解析——从 `OPPROF_*/OpBasicInfo.csv` 按 `Op Name` 前缀匹配出本算子的行，取 `Task Duration(us)` 均值（超过 warmup 数则跳过前 WARMUP 次）；从 `PipeUtilization.csv` 取 `aiv_vec_time/aiv_scalar_time/aiv_mte2_time/aiv_mte3_time` 各流水线耗时——**这四列正对应 u1-l4 讲的 MTE2 搬入、V 计算、MTE3 搬出流水线**，是判断「访存受限还是计算受限」的直接证据。

- [examples/08_rmsnorm/profile_msprof.py:141-164](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/profile_msprof.py#L141-L164)：`write_summary` 产出三后端耗时对比表（`pyasc_over_ascendc`、`pyasc_over_torch_npu` 两列比值），即 pyasc 生成代码质量相对原生 Ascend C 与框架算子的量化验收。

- [examples/08_rmsnorm/bench_rmsnorm.py:26-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/bench_rmsnorm.py#L26-L41)：被测负载 `run_pyasc`——预热加正式迭代循环调用 `rmsnorm_kernel[cores, rt.current_stream()](...)`，最后 `torch.npu.synchronize()`。msprof 采集的就是这批 launch；每一次 launch 都会经过上面 MsprofLauncher 的两个打点。

msprof 两种模式的命令与产物清单见：

- [docs/op_debug_prof.md:118-141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L118-L141)：上板采集 `msprof op --output=./output python add_framework.py -r NPU` 与 OPPROF 目录下的 CSV/bin 文件列表。
- [docs/op_debug_prof.md:145-175](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/op_debug_prof.md#L145-L175)：仿真采集 `msprof op simulator ... -r Model -v Ascend910B1` 与按 `core*.veccore*` 分目录的指令流水文件。

#### 4.4.4 代码实践

1. **实践目标**：跑通一次 msprof 采集（无 NPU 时用 simulator），并从 CSV 中读出自己算子的耗时与流水线占比。
2. **操作步骤**（仿真路径，无需 NPU）：
   ```bash
   cd examples/02_add_framework
   msprof op simulator --output=./prof_out python3 add_framework.py -r Model -v Ascend910B1
   find prof_out -name "*.csv" | head
   ```
   若有 NPU，则改用 `msprof op --output=./prof_out python3 add_framework.py -r NPU`。
3. **需要观察的现象**：`prof_out/OPPROF_*` 目录生成；上板模式可见 `OpBasicInfo.csv`、`PipeUtilization.csv`、`MemoryUB.csv` 等；仿真模式在 `simulator/core0.veccore0/` 下见 `*_instr_exe.csv` 与 `trace.json`。
4. **预期结果**：上板模式中 `OpBasicInfo.csv` 能按 `Op Name`（即 kernel 函数名）找到本算子行，`Block Dim` 等于 launch 用的核数（MsprofLauncher 上报的 blockNum）；`PipeUtilization.csv` 中 mte2/mte3 时间显著大于 vec 时间时，说明 Add 这类访存密集算子是搬运受限——与 u2-l4 双缓冲流水设计的动机互相印证。
5. 本机是否有 msprof、能否出数**待本地验证**（msprof 属 CANN 工具链，需按 quick_start 完成 CANN 安装）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MsprofLauncher.start/process` 在 Model 模式下直接 return，pyasc 在仿真器上仍能被 msprof 采集？
**答案**：Host 打点服务于上板时间线（区分 Host API 区间与任务信息）；仿真模式下性能数据来自仿真器自身对指令流的记录（simulator 目录下的 instr_exe/trace 文件），不需要 Host 侧 API 打点，故直接跳过以避免无意义开销。

**练习 2**：`OpBasicInfo.csv` 里的 `Op Name` 是从哪来的？
**答案**：来自 `launch_kernel` 调用 `msprof.process(func_name, ...)` 传入的函数名（launcher.py:115），经 `MsprofGetHashId` 哈希上报（npu_utils.cpp:149-161）。这也是 profile_msprof.py 里 `op_types = {"pyasc": "rmsnorm_kernel", ...}` 能按名字前缀匹配行的前提。

**练习 3**：不开 msprof 直接 `python3 add.py -r NPU`，打点会造成多少额外耗时？
**答案**：近乎为零。msprof 未启动时回调不会置位 `msprofFlagL0/L1`，`MsprofSysCycleTime` 直接返回 0、两个 Report 函数直接返回 1（npu_utils.cpp:88、103、137），只剩 Python 层两次方法调用的固定开销。

## 5. 综合实践

**任务：为 01_add 产出一分三段式调优报告（IR → C 代码 → profiling 数据）。**

无 NPU 时全部在 Model 模式完成（第三段用 simulator 或以三级 dump 分析替代）；有 NPU 时优先上板。

**步骤：**

1. **准备被测版本**：复制 `examples/01_add/add.py` 为 `add_report.py`（不改原文件），在 kernel 内加两处探针（示例代码）：`vadd_kernel` 开头 `asc.printf("core %d enter\\n", asc.get_block_idx())`；首轮循环内 `if i == 0: asc.dump_tensor(z_local[0:], 9, 32)`。
2. **第一段——IR**：`PYASC_DUMP_PATH=/tmp/report python3 add_report.py -r Model -v Ascend910B1`，打开 `codegen.mlir` 找到 `ascendc.Printf`/`ascendc.DumpTensor` 节点；diff `codegen.mlir` 与 `ascir.mlir`，记录 Pass 前后 Printf 节点位置是否移动（同步重建链可能在其前后插 Op）。
3. **第二段——C 代码**：在 `ascendc.cpp` 中标注三类内容：被注入的 `dump_addr` 形参与 `InitDump` 块（来源：`_gen_init_dump_code`）；`printf`/`DumpTensor` 对应的 `AscendC::printf`/`DumpTensor` 调用（来源：u6-l5 发射层）；`AscendC::Add` 主计算调用。三者各摘录一行进报告。
4. **第三段——profiling**：有 NPU 时 `msprof op --output=./prof_out python3 add_report.py -r NPU`，读取 `OpBasicInfo.csv` 的 Task Duration 与 `PipeUtilization.csv` 的 vec/mte2/mte3 时间；无 NPU 时执行 `msprof op simulator --output=./prof_out python3 add_report.py -r Model -v Ascend910B1`（msprof 可用时），或退而分析 `/tmp/report` 四级 dump + 运行日志中的 printf 输出，说明「仿真流水图缺失，仅完成三级 dump 分析」。
5. **结论段**：回答两个问题——(a) printf/dump_tensor 打开后耗时变化多少（对比不加探针的原版计时）？印证「仅调测期使用」的约束；(b) mte2/mte3 与 vec 的占比是否支持「Add 为搬运受限」的判断？据此给出一条改进方向（如调整 TILE_NUM/BUFFER_NUM，参见 u2-l4）。

**验收标准**：报告含三段证据各至少一条带文件路径的摘录、一个量化数字（耗时或占比）、一个明确结论。所有运行结果**待本地验证**。

## 6. 本讲小结

- **dump 链路**：`PYASC_DUMP_PATH` 让 `Compiler.run` 在四个节点落盘 `codegen.mlir`（Pass 前）、`ascir.mlir`（Pass 后）、`ascendc.cpp`（翻译+注入后）、`binary.o`（链接后）；只在真实编译时发生，缓存命中不刷新。
- **enable_debug 注入**：设备侧打印启用需四条件相与——源码含 printf/dump_tensor（DetectEnableDebugPass 打 `asc.enable_debug`）、`ASCENDC_DUMP` 环境变量不为 false、编译命令注入 `-DASCENDC_DUMP=1`、Launcher 追加 dump 缓冲；注入方式是对 C 源码文本追加 `dump_addr` 形参与 `#if defined ASCENDC_DUMP` 包裹的 `InitDump` 块。
- **print_utils**：75 MiB（1 MiB × 75 核）dump 工作区作为尾置隐藏参数下发，执行后最后一个 memory 参数不回拷而交 `PrintWorkSpace`（CANN `libascend_dump` 的 `Adx::AdumpPrintWorkSpace`）解析打印；so 按 sha256(源码+version.cfg) 在线编译并缓存。
- **调试选项分工**：`debug` 加 `-g` 服务指令级调试，`verify_sync` 挂队列配对检查，`print_ir_before_all` 逐 Pass 打印 IR，`strip_loc` 去位置信息；四者都进文件缓存 key。
- **msprof 打点**：`MsprofLauncher` 在 `rt.launch_kernel` 前后调 `msprof_report_api/compact_info` 上报算子名、核数、任务类型；生效与否由 msprof 回调置位的 L0/L1 开关决定，不开 msprof 零开销；Model 模式 Host 打点空转。
- **分析套路**：`PipeUtilization.csv` 的 vec/mte2/mte3 三列直接对应搬运-计算-搬运流水线，是判断访存/计算受限的第一手证据。

## 7. 下一步学习建议

- **u7-l5（开发者工具）**：学习用 `ascir-opt` 单独跑某个 Pass、`ascir-translate` 手工翻译 IR，把本讲的「对照阅读」从整链路细化到单 Pass 粒度。
- **u7-l6（测试与贡献）**：为你的算子补 pytest 回归，理解 `build_llt.sh` 的三层测试体系，把调优结论沉淀成可守护的用例。
- **延伸阅读**：docs/op_debug_prof.md 的「Ascend PyTorch Profiler」一节（框架级采集）与 MindStudio Insight 的可视化用法；再对照 `examples/08_rmsnorm/README.md` 看一个完整的三后端对比实验是如何组织的。
