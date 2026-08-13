# npu check 错误体系与检查机制

## 1. 本讲目标

本讲聚焦 asc-tools 五大工具中的 **npu check（NPU 校验）**。学完本讲后你应该能够：

- 说清 npu check 与 cpu debug、api_check 三者的协作关系——谁先跑、谁依附谁、各自的产物是什么。
- 在 `RunKernelFunctionOnCpu` 的 fork 执行流程里指出 npu check 的挂载点，理解它为何「每核一份日志」。
- 逐条解释 `ErrorRead / ErrorWrite / ErrorSync / ErrorLeak / ErrorFree / ErrorBuffer` 六大类错误码的含义与典型触发场景。
- 理解 VECIN/VECOUT/VECCALC 类型 Tensor 的 `AllocTensor→EnQue→DeQue→FreeTensor` 生命周期状态机，以及多核写 GM「踩踏」是如何被判定的。
- 能够亲手在 add 样例里构造一个错误，用 `ascendc_npuchk_report.py` 把 `*_npuchk.log` 解析回源码行。

## 2. 前置知识

本讲建立在前面几讲之上，复习要点如下：

- **孪生调试（u1-l1 / u2-l1）**：用 CPU 构造 NPU 行为的孪生体，同一份 Ascend C 源码不改一行就能在 CPU 域跑通。`<<<>>>` 在编译期被 lowering 成对 `AscCPUKernelLaunch` 的调用，后者转交 `RunKernelFunctionOnCpu`。
- **多核 fork 执行模型（u3-l1）**：`RunKernelFunctionOnCpu` 用 `fork()` 为每个 block 产生一个子进程来模拟 NPU 多核，父进程用 `waitpid` 回收。核函数 `add_custom → Process → CopyIn/Compute/CopyOut` 全部跑在 `pid==0` 的子进程分支里。
- **Stub 注册（u3-l3）**：cpudebug 维护一张 `(fid, type)` 二维函数表，三个前缀 `AscendC / cceprint / npuchk` 分别对应「功能实现 / 打印跟踪 / 运行时校验」三类 stub，其中 `npuchk` 类由构建期脚本 `write_npuchk.py` 生成。**这正是本讲 npu check 的「触手」来源**。
- **API 校验框架（u4-1/u4-l2/u4-l3）**：`api_check` 模块在调用内建函数时校验参数（scope、对齐、repeat/mask/stride、Tensor 越界），违例经 `ASCENDC_CHECK` 宏短路返回。这正是 npu check 文档里所说的「debug 功能」。

一个最容易混淆的点：**本讲的 npu check 不等于 u4 的 api_check**。4.1 节会把两者的边界讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/02_npu_check.md` | npu check 官方说明，定义了全部错误码、EnQue/DeQue 与 GM 踩踏两类专项检查，以及构造错误用例的完整步骤。 |
| `npuchk/ascendc_npuchk_report.py` | 日志解析脚本：把 `*_npuchk.log` 里的错误码、`###` 指令行、堆栈地址还原成可读的「错误码 + Rule + 源码行」并打屏。 |
| `npuchk/CMakeLists.txt` | 安装脚本：把 `ascendc_npuchk_report.py` 装进 run 包的 `tools/ascendc_tools/` 或 `lib64` 路径。 |
| `cpudebug/include/kern_fwk.h` | npu check 的「挂载点」：`RunKernelFunctionOnCpu` 用 `#ifndef ASCENDC_NPUCHK_OFF` 把校验调用裹在 fork 模型的关键位置。 |
| `cpudebug/src/regfwk/stub_reg.cpp` | 注册表前缀数组 `g_regStubs`，其中的 `"npuchk"` 前缀驱动运行时校验 stub 的绑定。 |
| `cpudebug/cmake/fun.cmake` | 构建期生成 npuchk stub 的脚本入口，调用闭源的 `write_npuchk.py`。 |
| `examples/02_cpudebug/add.asc` | 实践任务用的算子样例，含完整的 `AllocTensor/EnQue/DeQue/FreeTensor` 生命周期。 |

> **开源/闭源边界提示**：npu check 的**检查引擎本体**（`AscendCKernelBegin`、`CheckSyncState`、`CheckGmValied` 等符号的实现）位于闭源模型库 `libraries/lib/libcpudebug_npuchk.so`，不在仓库源码中。本讲能读到的是它的「**触发入口**」（`kern_fwk.h`）与「**日志出口**」（`ascendc_npuchk_report.py`），中间的检测逻辑不可见但可由日志格式反推。

## 4. 核心概念与源码讲解

### 4.1 npu check 触发机制与协作关系

#### 4.1.1 概念说明

文档开篇一句话就给 npu check 定了位（[docs/02_npu_check.md:3-5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L3-L5)）：

> Ascend C Tools 提供的孪生调试分为 **debug 功能**和 **npu check 功能**，debug 功能包含诸如是否合法使用接口、参数校验等，**在此之上** npu check 提供了内存检查、内存生命周期管理、内存地址依赖管理、同步事件管理等功能。

把这句话拆成三层，就能理清三个概念的关系：

| 层 | 模块 | 检查什么 | 何时检查 | 产物 |
| --- | --- | --- | --- | --- |
| 1 | **cpu debug** | 让算子在 CPU 域跑起来 | 编译 + 运行 | 可执行文件 + 仿真执行 |
| 2 | **api_check（debug 功能）** | 接口参数是否合法（scope/对齐/越界/mask） | 调用内建函数的瞬间 | `ASSERT` 失败则中断 |
| 3 | **npu check（本讲）** | 内存生命周期、地址依赖、同步、多核踩踏 | 算子执行全程同步跟踪 | `*_npuchk.log` |

关键区别：**api_check 是静态的、调用点的参数校验**（u4 讲的 repeat/mask/stride 数学模型），而 **npu check 是动态的、跨指令的运行时跟踪**——它要知道「这块内存是不是先 alloc 再用的」「上一个流水级有没有给它发同步事件」「另一个核是不是在写同一片 GM」。前者像门卫查证件，后者像全程跟拍的行车记录仪。

两者还有一个硬约束（[docs/02_npu_check.md:5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L5)）：

> 只有当 debug 阶段正常退出（即未有 ASSERT 校验），npu check 才会输出完整的校验日志及分析。

也就是说，api_check 一旦 `ASSERT` 失败提前中断，npu check 的日志就不完整。这给出了排查顺序：**先清掉 api_check 的 ASSERT，再看 npu check 的 Error**。

#### 4.1.2 核心流程

npu check 不需要单独启动，它「寄生」在 cpu debug 的执行流程上。结合 u3-l1 的 fork 模型，它的挂载点分布如下：

```
父进程 (g_mainPid)
 │
 ├─ StubInit()                          # 填充 (fid,type) 函数表，含 npuchk 类 stub
 ├─ AscendCKernelBegin(...)             # 【npuchk】kernel 级初始化（父进程，跑一次）
 ├─ for idx in processNum:
 │     fork() ─────────────┐
 │   父: waitpid            │  子进程 (模拟核 idx, pid==0)
 │                          ├─ AscendCBlockBegin(...)   # 【npuchk】block 级初始化
 │                          ├─ CheckGmValied(...)       # 【npuchk】校验 GM 参数合法性
 │                          ├─ try { kernelFunc(...) }  # 真正跑算子
 │                          │      AscendC::CheckSyncState()  # 【npuchk】结束时查同步
 │                          ├─ catch logic_error → 打印 [NPUCHECK ERROR] 并退出
 │                          └─ AscendCBlockEnd(...)     # 【npuchk】block 级收尾
 │
 └─ AscendCKernelEnd(...)               # 【npuchk】kernel 级收尾，落盘 *_npuchk.log
```

要点：

1. **kernel 级**（`AscendCKernelBegin/End`）只在父进程跑一次，负责整个 kernel 的初始化与日志汇总。
2. **block 级**（`AscendCBlockBegin/End`）在每个 fork 出来的子进程里跑一次，对应一个模拟核——所以多核用例会产生**多份** `*_npuchk.log`（每核一份）。
3. **同步检查** `CheckSyncState` 在 `kernelFunc` 正常返回后调用；若 npu check 引擎抛出 `std::logic_error`，会被 catch 转成 `[NPUCHECK ERROR]` 打印后 `exit(-1)`。
4. 整个 npuchk 调用链都被 `#ifndef ASCENDC_NPUCHK_OFF` 包裹，定义该宏即可整体关停（见 4.1.3）。

#### 4.1.3 源码精读

**① 挂载点：`RunKernelFunctionOnCpu` 里的 npuchk 调用**

[cpudebug/include/kern_fwk.h:96-100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L96-L100) 是 kernel 级入口：`StubInit` 之后、fork 循环之前，调用 `AscendCKernelBegin` 开启 kernel 级跟踪。

```cpp
AscendC::StubInit();
#ifndef ASCENDC_NPUCHK_OFF
    AscendCKernelBegin(funcName, argn, kargs);
    AscendCNpuCheckEnInterruptExit();
#endif
```

[cpudebug/include/kern_fwk.h:130-148](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L130-L148) 是 block 级核心，位于 `if (pid == 0)` 的子进程分支内（[kern_fwk.h:117](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L117)），每核跑一次：先 `AscendCBlockBegin` 登记 block，再 `CheckGmValied` 校验传入的 GM 参数，然后把整个 `kernelFunc` 包在 `try/catch` 里：

```cpp
#ifndef ASCENDC_NPUCHK_OFF
    AscendCBlockBegin(static_cast<int32_t>(block_idx), funcName, argn, kargs);
#endif
    AscendC::CheckGmValied(argn, kargs);
#ifndef ASCENDC_NPUCHK_OFF
    try {
        kernelFunc(args...);
        AscendC::CheckSyncState();
    } catch (std::logic_error& err) {
        std::cout << "[NPUCHECK ERROR]: " << err.what() << std::endl;
        AscendCBlockEnd(static_cast<int32_t>(block_idx), funcName, argn, kargs);
        exit(-1);
    }
    AscendCBlockEnd(static_cast<int32_t>(block_idx), funcName, argn, kargs);
#else
    kernelFunc(args...);
    AscendC::CheckSyncState();
#endif
```

注意 `#else` 分支：关停 npuchk 后，`kernelFunc` 仍照常执行，只是少了 `AscendCBlockBegin/End` 的跟踪——这正是 `ASCENDC_NPUCHK_OFF` 的作用：**只摘掉检查、不影响算子功能仿真**。

收尾在父进程回收完所有子进程后（[cpudebug/include/kern_fwk.h:172-182](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L172-L182)）：调 `AscendCKernelEnd` 汇总，并释放 `KernelPrintLock/ProcessLock`（这两个跨进程锁见 u3-l1）。

```cpp
#ifndef ASCENDC_NPUCHK_OFF
        AscendCKernelEnd(funcName, argn, kargs);
#endif
    ...
#ifndef ASCENDC_NPUCHK_OFF
    AscendC::KernelPrintLock::FreeLock();
    AscendC::ProcessLock::FreeLock();
#endif
```

**② 触手来源：`npuchk` 前缀 stub**

[cpudebug/src/regfwk/stub_reg.cpp:25-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/regfwk/stub_reg.cpp#L25-L29) 的前缀数组第三个元素 `"npuchk"` 就是本讲的触手（u3-l3 详述过注册机制）：

```cpp
const char* g_regStubs[INTRI_TYPE_MAX]{
    "AscendC",   // 功能实现
    "cceprint",  // 打印跟踪
    "npuchk",    // 运行时校验 ← 本讲
};
```

也就是说，算子里每调一次 `DataCopy`、`Add`，除了真正干活的 `AscendC` 实现，还会同时触发同名 `npuchk` 版 stub 做内存/同步检查。这些 stub 由构建期脚本生成：[cpudebug/cmake/fun.cmake:30-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L30-L32) 调用闭源的 `write_npuchk.py`：

```cmake
function(gen_npuchk_stub target config stub_cc)
  gen_cmd_common(${target} "${CPULIB_SRC_DIR}/model/scripts/write_npuchk.py" ${config} ${stub_cc})
endfunction()
```

最终产物 `libcpudebug_npuchk.so`（闭源）通过 [cpudebug/CMakeLists.txt:245-249](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L249) 一并安装，并在 [cpudebug/CMakeLists.txt:265-270](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L265-L270) 建立 `libtikicpulib_npuchk.so` 兼容软链（旧名兼容，机制见 u1-l4）。

**③ 安装：脚本如何进入 run 包**

[npuchk/CMakeLists.txt:10-21](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/CMakeLists.txt#L10-L21) 把 `ascendc_npuchk_report.py` 装到 `tools/ascendc_tools/`（开源工程模式）或 `${INSTALL_LIBRARY_DIR}`（普通模式），并赋予可执行权限。这就是安装后你能直接 `python3 ascendc_npuchk_report.py` 的原因。

#### 4.1.4 代码实践

**实践目标**：追踪 npu check 在 fork 模型里的挂载位置，理解「每核一份日志」的来源。

**操作步骤（源码阅读型）**：

1. 打开 [cpudebug/include/kern_fwk.h:111-151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L111-L151)，确认 `AscendCBlockBegin` 出现在 `if (pid == 0)` 分支内、即子进程里。
2. 对照 docs 的使用示例（[docs/02_npu_check.md:124-130](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L124-L130)）：多核用例生成的日志名为 `add_custom_0_0_vec_npuchk.log`，其中的 `0_0` 对应「block 0 / 子序号 0」。
3. 思考：add 样例 `NUM_BLOCKS=8`（[examples/02_cpudebug/add.asc:26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L26)），但 u3-l1 提到 `get_process_num()` 与 `numBlocks` 解耦，实际会产生几份日志？

**需要观察的现象**：npuchk 调用全部出现在「子进程分支」内，因此日志按核分裂。

**预期结果**：日志份数取决于实际 fork 数（`processNum`，[kern_fwk.h:102](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L102)），而非 `numBlocks`；`add_custom_<block>_<seq>_vec_npuchk.log` 的命名规律印证了「一核一文件」。

> 待本地验证：`get_process_num()` 在你的机器上返回值与实际产生的日志份数。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ASCENDC_NPUCHK_OFF` 宏定义打开，算子还能在 CPU 域正常执行吗？日志会怎样？

> **答案**：能正常执行。`#else` 分支显示 `kernelFunc` 照常调用，只是 `AscendCBlockBegin/End`、`CheckGmValied` 的跟踪被摘掉，因此不会生成 `*_npuchk.log`，自然也解析不出任何 Error。

**练习 2**：api_check 与 npu check 的产物分别是什么？为什么说前者是后者的「前置条件」？

> **答案**：api_check 产物是调用点的 `ASSERT`（失败即中断运行）；npu check 产物是 `*_npuchk.log`。因为 ASSERT 失败会让算子提前中断，npu check 无法完成全程跟踪，故文档要求「debug 阶段正常退出（未有 ASSERT）」才输出完整 npuchk 日志。

---

### 4.2 错误类型体系

#### 4.2.1 概念说明

npu check 把所有违例抽象成一个扁平的错误码集合，共 **6 大类、19 个错误码**。它们都遵循统一命名 `Error<类别><序号>`，序号在同一类别内从 1 递增。掌握这套命名，看到错误码就能反推它查的是什么：

| 类别 | 检查的维度 | 典型问题 |
| --- | --- | --- |
| `ErrorRead1~4` | **读访问**合法性 | 读未申请/已释放、读未初始化、读越界、读未对齐 |
| `ErrorWrite1~4` | **写访问**合法性 | 写未申请/已释放、写越界、重复写、写未对齐 |
| `ErrorSync1~4` | **流水同步**正确性 | pipe 内/间缺 barrier、set/wait 不配对、eventID 重复 |
| `ErrorLeak` | 内存**泄漏** | 申请了未释放 |
| `ErrorFree` | 内存**重复释放** | 对同一块调两次 free |
| `ErrorBuffer0~4` | **TQue/TBuf 生命周期** | 未 InitBuffer、que 类型不一致、VECIN/OUT/CALC 操作不合规等 |

其中标 `[可疑问题]` 的（`ErrorRead2`、`ErrorWrite3`）是**非致命**提示——读到了从没写过的内存、或重复写入未被取走的数据，不一定是 bug 但高度可疑。

#### 4.2.2 核心流程

一个错误码从产生到展示给开发者，跨三处：

```
闭源 npuchk 引擎 (libcpudebug_npuchk.so)        *_npuchk.log              ascendc_npuchk_report.py
        │                                            │                            │
  检测到违例 ──────────────────────────────►  写入一行 [ErrorXxx] ──────────► parse_log 提取错误码
        │                                  写入 ### 指令行              addr2line+c++filt 还原堆栈
        │                                  写入 # BackTrace # 地址表     按 err_details 查 Rule 打屏
```

1. **检测**：npuchk stub 在算子执行时发现违例（闭源，不可见）。
2. **落盘**：违例信息按固定格式写入 `*_npuchk.log`，共三类行：
   - 错误行：含 `[ErrorXxx]` 标记；
   - 指令行：以 `### ` 开头，记录触发错误的那条内建函数调用及其参数；
   - 堆栈行：`# BackTrace #` 标记后，若干缩进行，格式 `二进制文件(符号+0x地址)`。
3. **解析**：`ascendc_npuchk_report.py` 读日志，用 `addr2line` + `c++filt` 把地址映射回源码函数与行号，再按内置字典 `err_details` 给出每条错误的「Rule」（规则解释）。

#### 4.2.3 源码精读

**① 错误码权威定义：文档**

[docs/02_npu_check.md:42-79](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L42-L79) 逐条列出了全部错误码及含义，是本讲的权威字典。例如：

```text
- ErrorRead1: 非法内存读取数据：整段内存未经过Ascend C框架的AllocTensor申请或已被FreeTensor。
- ErrorWrite1: 非法内存写入数据，未经过Ascend C框架的AllocTensor申请或已被FreeTensor。
- ErrorBuffer2: VECIN/VECOUT/VECCALC的操作不合规。
```

注意描述里反复出现的「**未经 Ascend C 框架的 AllocTensor 申请**」——这是 npu check 与 api_check 的根本不同：它跟踪的是**框架记账**下的内存生命周期，而不是裸指针。

**② 错误码字典：脚本的 `err_details`**

[npuchk/ascendc_npuchk_report.py:112-132](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L112-L132) 用一个 dict 复刻了文档的错误码到「Rule」的映射，这是打屏时「Rule：……」那一行的来源：

```python
err_details = {
    "ErrorRead1": "非法内存读取数据: 整段内存未经过AscendC框架的alloc_buf申请或者已free",
    ...
    "ErrorBuffer2": "VECIN/VECOUT/VECCALC的操作不合规",
    "ErrorBuffer0": "tensor内存未使用Ascendc框架的bufInit",
    ...
}
```

> 旁注：脚本里 `ErrorBuffer4` 的文案写作「ButPool 资源池…」（应为 `BufPool` 的笔误），而文档写作「TBufPool 资源池」——这是开源脚本与文档之间的一处轻微漂移，阅读时以文档措辞为准。

**③ 错误码提取：`get_error_type`**

[npuchk/ascendc_npuchk_report.py:23-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L23-L31) 从一行文本里抠出 `[ErrorXxx]` 标记，定位逻辑很简单：找 `[Error`、再找下一个 `]`，中间就是错误码。

```python
def get_error_type(info_input):
    err_start = info_input.find("[Error")
    if err_start < 0:
        return None
    err_info_str = info_input[err_start + 1 :]
    err_stop = err_info_str.find("]")
    ...
    return err_info_str[:err_stop]
```

**④ 日志解析：`parse_log`**

[npuchk/ascendc_npuchk_report.py:34-71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L34-L71) 是解析主循环，逐行扫描，靠三个特征识别上文说的三类行：

- 命中 `[Error...]` → 记一条错误，并把上一条缓存的 `### ` 指令行挂上；
- 行首 `### ` → 缓存为当前错误对应的指令行；
- `# BackTrace #` → 开启堆栈段，其后每行 `二进制(符号+0x地址)` 拆成 `二进制:地址` 存入；含 `.so` 的行跳过。

```python
if line.startswith("### "):
    cce_intri = line.strip()
    ...
if bs_start:
    if line.find(".so") > 0:
        continue
    addr = line.split("+")[1].split(")")[0]
    info_tmp = binfile + ":" + addr
```

**⑤ 地址→源码：`addr_to_line`**

[npuchk/ascendc_npuchk_report.py:90-100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L90-L100) 用 `addr2line -f` 取函数名和行号，再用 `c++filt` 把 C++ 修饰名（mangled name，如 `_ZN7AscendC3AddEE...`）还原成可读函数名：

```python
def addr_to_line(bin_file, addr):
    res = execute_cmd(["addr2line", "-f", "-e", bin_file, addr])
    fun_line = res.split("\n")
    ...
    fun = execute_cmd(["c++filt", fun])
    ...
    return "{} at {}".format(fun, line)
```

这套 `addr2line + c++filt` 与闭源 `stub_backtrace.cpp` 里 `dladdr` + `addr2line` 的逆向回溯（u3-l3）是同一套机制的两端：引擎侧落地址，脚本侧译回源码。

#### 4.2.4 代码实践

**实践目标**：用一条假日志走通「错误码提取 → Rule 查找」的全流程，无需真机。

**操作步骤**：

1. 准备一个最小日志文件 `/tmp/fake_npuchk.log`，内容照抄文档示例（[docs/02_npu_check.md:26-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L26-L32)）：

   ```text
   [V] [ErrorRead3] on read 0x7f328c11b010 0x800B
   ### vadd((__ubuf__ half*)7f328c11b810, (__ubuf__ half*)0xf328c11b010, (__ubuf__*)0x7f328c11b410, (uint8_t)1, (uint8_t)1, (uint8_t)1, (uint8_t)1, (uint8_t)8, (uint8_t)8, (uint8_t)8);
   ```

2. 阅读脚本 main 主流程 [npuchk/ascendc_npuchk_report.py:143-178](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L143-L178)，跟踪：`parse_log` 填 `err_stack` → 逐错误取 `err_type` → 累加 `stats` 计数 → 拼 `Rule` → 打屏。
3. 思考：当堆栈行为空（像本假日志没有 `# BackTrace #`），脚本走到 [npuchk/ascendc_npuchk_report.py:157-161](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L157-L161) 的 `if len(cur_err_info) <= 2: continue` 会跳过地址翻译，只打错误码 + Rule。

**需要观察的现象**：即便没有真实二进制，错误码与 Rule 仍能正确输出；`addr2line` 只在堆栈行存在时才被调用。

**预期结果**：打屏出现 `ErrorRead3` 与「Rule: 读取越界…」，最后跟一段 `ERROR STATISTICS` 汇总。

> 待本地验证：在你机器上跑 `python3 npuchk/ascendc_npuchk_report.py /tmp/fake_npuchk.log` 的实际输出。

#### 4.2.5 小练习与答案

**练习 1**：`ErrorRead2` 和 `ErrorWrite3` 为什么被标注 `[可疑问题]`？它们与其同类别的其他错误码有何本质区别？

> **答案**：它们检测的是「读未初始化」「重复写入」——这些在语义上**不一定是 bug**（也许开发者刻意如此），但高度可疑，故只提示不致命。而 `ErrorRead1/3/4`、`ErrorWrite1/2/4` 检测的是确定性的越界/非法访问，属于硬错误。

**练习 2**：脚本如何区分「同一种错误出现多次」？看哪段代码？

> **答案**：看 [npuchk/ascendc_npuchk_report.py:150-154](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L150-L154)：用 `stats` 字典按 `err_type` 计数，最后 [npuchk/ascendc_npuchk_report.py:177-178](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L177-L178) 以 `计数, 错误码, Rule` 三元组汇总打屏。

---

### 4.3 EnQue/DeQue 状态机与 GM 多核踩踏检查

#### 4.3.1 概念说明

错误体系里有两组检查最贴近实际开发踩坑，值得单独成节：**Tensor 生命周期状态机**（对应 `ErrorBuffer*` 与部分 `ErrorRead/Write`）和 **GM 多核踩踏**。

**Tensor 生命周期状态机**：Ascend C 的 `TQue` 通过四个原语管理 `LocalTensor`（u2-l2 已介绍）：

```
AllocTensor ──► EnQue ──► DeQue ──► FreeTensor
 (申请)      (入队待算)  (出队取用)   (释放)
```

每个 `LocalTensor` 在任一时刻都处在某个状态。npu check 会校验：当一个 VECIN/VECOUT/VECCALC 类型的 Tensor 出现在搬运/计算指令里时，它**是否处在被允许的状态**。文档把这一类违例归为 `ErrorBuffer2: VECIN/VECOUT/VECCALC 的操作不合规`（[docs/02_npu_check.md:74-75](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L74-L75)）。最常见的违规就是**对一个已经 `FreeTensor` 的 Tensor 继续读写**——这会同时触发 `ErrorBuffer2`（状态不合规）和 `ErrorWrite1/Read1`（访问已释放内存）。

**GM 多核踩踏**：NPU 上多个核共享 HBM（GM）。如果两个核写了**重叠的 GM 地址区间**，就是数据竞争（「踩踏」）。npu check 记录每个核的 GM 写入范围，发现重叠就报错；但 **Atomic add** 场景下重叠是合法的（累加语义天然允许并发写同地址），故不报。见 [docs/02_npu_check.md:85-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L85-L87)。

#### 4.3.2 核心流程

**Tensor 状态机的合法迁移**：

| 当前状态 | 允许的操作 | 违规会怎样 |
| --- | --- | --- |
| 未 Alloc / 已 Free | （都不能用） | 读写 → `ErrorRead1/ErrorWrite1`；出现在指令 → `ErrorBuffer2/3` |
| Alloc 后、EnQue 前 | 可搬运写入（CopyIn） | 此时若 Free → 后续读写变非法 |
| EnQue 后、DeQue 前 | 不应再写 | 重复写 → `ErrorWrite3`（可疑） |
| DeQue 后 | 可参与计算（Compute） | 此时算完应 Free |

**GM 踩踏的数学判定**：设核 A 写区间 \([a, a+l_a)\)、核 B 写区间 \([b, b+l_b)\)（左闭右开，\(l\) 为字节长度）。两者重叠当且仅当：

\[
a < b + l_b \quad \land \quad b < a + l_a
\]

等价地，两区间起点之差的绝对值小于较长区间的长度。npu check 对所有核的两两写区间套用上式，命中即报错（Atomic add 除外）。

#### 4.3.3 源码精读

**① EnQue/DeQue 检查（文档定义）**

[docs/02_npu_check.md:81-83](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L81-L83)：

```text
对于VECIN/VECOUT/VECCALC类型的Tensor，判断Tensor出现在搬运/计算指令时是否处于正确的状态，
以保证同步的正确性，对于异常的状态，会在日志中记录。
```

注意「以保证同步的正确性」——状态机违例不仅是内存问题，还会破坏流水同步（`TQue` 的 `EnQue/DeQue` 本身就是 MTE2↔Vector↔MTE3 间的同步信号）。这也是为什么 `ErrorBuffer2` 既归入 Buffer 类，又与同步强相关。

**② GM 踩踏检查（文档定义 + 运行时入口）**

[docs/02_npu_check.md:85-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L85-L87)：

```text
基于GM全局内存的管理机制，记录每个核操作的GM地址范围，发现多核写入地址范围有重叠的情况，记录错误；
支持Atomic add场景下，对于重叠地址不记录错误。
```

它的运行时入口是 [cpudebug/include/kern_fwk.h:133](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L133) 的 `AscendC::CheckGmValied(argn, kargs)`——在每个子进程（核）开始执行算子前，先校验传入的 GM 参数（地址范围）。`CheckGmValied` 本体在闭源 npuchk 库内。

**③ 健康的生命周期样例：add.asc**

正确的成对用法见 [examples/02_cpudebug/add.asc:55-79](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L55-L79)：

```cpp
// CopyIn: Alloc → DataCopy(写) → EnQue
AscendC::LocalTensor<float> xLocal = inQueueX.AllocTensor<float>();
AscendC::DataCopy(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH);
inQueueX.EnQue(xLocal);
// Compute: DeQue → Add(算) → EnQue(z) → Free(x/y)
AscendC::LocalTensor<float> xLocal = inQueueX.DeQue<float>();
AscendC::Add(zLocal, xLocal, yLocal, TILE_LENGTH);
inQueueX.FreeTensor(xLocal);
// CopyOut: DeQue(z) → DataCopy(读) → Free(z)
AscendC::LocalTensor<float> zLocal = outQueueZ.DeQue<float>();
AscendC::DataCopy(zGm[progress * TILE_LENGTH], zLocal, TILE_LENGTH);
outQueueZ.FreeTensor(zLocal);
```

这正是 4.3.2 状态机表里每一行的正面教材。本讲实践任务就是要**故意打破**它。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：在 add 样例的 `CopyIn` 里提前 `FreeTensor`，构造生命周期违例，跑通「编译 → 运行 → 解析日志」全链路，观察 `ErrorBuffer2` 与 `ErrorWrite1`。

> **关于样例类型的一点说明**：`docs/02` 的步骤说明里贴的代码片段用的是 `half`（见 [docs/02_npu_check.md:98-108](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L98-L108)，且指向 asc-devkit 的另一份 add 样例），而本仓 `examples/02_cpudebug/add.asc` 用的是 `float`。两者只是元素类型不同，`FreeTensor` 的构造方法完全一致——下面以本仓真实源码（`float`）为准。

**操作步骤**（严格对照 [docs/02_npu_check.md:94-110](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L94-L110)）：

1. **构造错误**：编辑 [examples/02_cpudebug/add.asc:57-62](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L57-L62) 的 `CopyIn`，在 `AllocTensor` 之后、`DataCopy` 之前插入一行 `FreeTensor`：

   ```cpp
   AscendC::LocalTensor<float> xLocal = inQueueX.AllocTensor<float>();
   AscendC::LocalTensor<float> yLocal = inQueueY.AllocTensor<float>();
   // 此处增加以下一行代码来构造错误示例
   inQueueX.FreeTensor(xLocal);
   // 剩余代码保持不变
   AscendC::DataCopy(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH);
   ...
   inQueueX.EnQue(xLocal);
   ```

2. **编译运行生成 log**：参考 u2-l1/u1-l4，在样例目录用 CPU 模式编译并运行（命令形态见 [docs/02_npu_check.md:116-122](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L116-L122)）。运行后在执行路径下的 `npuchk/` 文件夹里会生成 `add_custom_<block>_<seq>_vec_npuchk.log`。
3. **解析日志**：执行

   ```bash
   python3 ${git_clone_path}/asc-tools/npuchk/ascendc_npuchk_report.py npuchk/add_custom_0_0_vec_npuchk.log
   ```

   不指定文件时，脚本会自动递归搜索 `**/*_npuchk.log`（见 [npuchk/ascendc_npuchk_report.py:141](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L141)）。

**需要观察的现象**：

- `xLocal` 被 `FreeTensor` 后，紧接着的 `DataCopy(xLocal, ...)` 是对**已释放内存**的写入。
- 稍后的 `EnQue(xLocal)` 也把一个已释放的 Tensor 入队。

**预期结果**（对照 [docs/02_npu_check.md:140-144](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L140-L144)）：打屏出现两条例子统计：

```text
----------------------ERROR STATISTICS----------------------
1，ErrorBuffer2，VECIN/VECOUT/VECCALC的操作不合规
1，ErrorWrite1，非法内存写入数据：未经过Ascend A框架的alloc_buf申请或已经free
```

- `ErrorWrite1`：对已 `FreeTensor` 的 `xLocal` 执行 `DataCopy` 写入 → 非法内存写入。
- `ErrorBuffer2`：`xLocal`（VECIN 类型）在被 `Free` 后仍出现在搬运指令里 → VECIN 操作不合规。

**如何读懂堆栈**：脚本会把 `[ErrorXxx]` 行、`### DataCopy(...)` 指令行、以及经 `addr2line` 译回的源码位置一并打出，你可据此定位到 `add.asc` 的具体行。

> 待本地验证：不同 SoC/核数下实际报错计数与日志文件名后缀。

#### 4.3.5 小练习与答案

**练习 1**：为什么 4.3.4 的实践同时报出 `ErrorBuffer2` 和 `ErrorWrite1` 两个错误，而不是只有一个？

> **答案**：同一动作触犯了两条规则。`DataCopy(xLocal,…)` 中 `xLocal` 是 VECIN 类型且已被 Free，从「Tensor 状态机」角度看是 VECIN 操作不合规（`ErrorBuffer2`），从「写访问合法性」角度看是对已释放内存的写入（`ErrorWrite1`）。npu check 的多个检查维度是**正交并行**的，一个动作可同时命中多条。

**练习 2**：两个核分别写 GM 区间 \([0, 512)\) 与 \([256, 768)\)（字节），是否构成踩踏？若改用 Atomic add 呢？请用 4.3.2 的公式验证。

> **答案**：\(a=0,l_a=512,b=256,l_b=512\)。代入：\(0 < 256+512=768\) 且 \(256 < 0+512=512\)，两式皆真 → **重叠，构成踩踏**，会报错。若为 Atomic add，文档明确「对于重叠地址不记录错误」，故不报。

**练习 3**：`ErrorLeak`（内存泄漏）在 add 样例里如何触发？提示：看 `Compute`/`CopyOut` 里的 `FreeTensor`。

> **答案**：删掉 `Compute` 里的 `inQueueX.FreeTensor(xLocal)`（[add.asc:71](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L71)）或 `CopyOut` 里的 `outQueueZ.FreeTensor(zLocal)`（[add.asc:78](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L78)），使 `AllocTensor` 没有配对的 `FreeTensor`。kernel 结束时 npu check 发现「申请未释放」，即报 `ErrorLeak`。

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的「**错误注入 → 日志生成 → 源码定位**」闭环：

1. **通读挂载点（4.1）**：在 [cpudebug/include/kern_fwk.h:96-182](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L96-L182) 标注出 `AscendCKernelBegin / AscendCBlockBegin / CheckGmValied / CheckSyncState / AscendCBlockEnd / AscendCKernelEnd` 六个 npuchk 调用点，画出它们与 fork 循环的对应关系（哪些在父进程、哪些在子进程）。
2. **注入两类错误（4.2 + 4.3）**：在 add 样例里分别制造——
   - 生命周期违例：`CopyIn` 中提前 `FreeTensor`（预期 `ErrorBuffer2` + `ErrorWrite1`）；
   - 内存泄漏：注释掉 `CopyOut` 的 `outQueueZ.FreeTensor(zLocal)`（预期 `ErrorLeak`）。
3. **解析并定位（4.2）**：对生成的 `*_npuchk.log` 分别跑 `ascendc_npuchk_report.py`，核对打屏的错误码与 Rule 是否与 `err_details` 字典一致，并用堆栈里的 `addr2line` 结果回指到 `add.asc` 的具体行。
4. **验证 Atomic 豁免（4.3，进阶）**：阅读 [docs/02_npu_check.md:85-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L85-L87)，结合 4.3.2 的区间重叠公式，说明为何多核 Atomic add 写同一地址不报踩踏。

> 待本地验证：第 2、3 步的实际编译运行与日志输出。

## 6. 本讲小结

- npu check 是**叠在 cpu debug / api_check 之上的运行时检查层**：api_check 查调用点参数（ASSERT），npu check 全程跟踪内存生命周期、地址依赖、同步、多核踩踏（产物 `*_npuchk.log`）；只有 api_check 不报 ASSERT 时 npu check 日志才完整。
- npu check **没有独立启动入口**，它「寄生」在 `RunKernelFunctionOnCpu` 上：`#ifndef ASCENDC_NPUCHK_OFF` 用 `AscendCKernelBegin/End`（kernel 级）、`AscendCBlockBegin/End`（每核 block 级）、`CheckGmValied`、`CheckSyncState` 把校验裹在 fork 模型的关键位置；检查引擎本体在闭源 `libcpudebug_npuchk.so`。
- 它的「触手」是 u3-l3 注册表里的 `"npuchk"` 前缀 stub，由构建期 `write_npuchk.py` 生成；算子每调一次内建函数都会同时触发对应的 npuchk 校验。
- 错误体系是 6 大类、19 个扁平错误码：`ErrorRead/Write（各 4）/ Sync（4）/ Leak / Free / Buffer（5）`，其中 `[可疑问题]` 标记的 `ErrorRead2 / ErrorWrite3` 非致命。
- `ascendc_npuchk_report.py` 的解析三件套：`get_error_type` 抠错误码、`parse_log` 抓 `###` 指令行与堆栈地址、`addr_to_line` 用 `addr2line + c++filt` 译回源码行。
- EnQue/DeQue 状态机（`AllocTensor → EnQue → DeQue → FreeTensor`）违例归为 `ErrorBuffer2`；GM 多核写区间重叠用 \([a,a+l_a) \cap [b,b+l_b)\) 判定，Atomic add 豁免。

## 7. 下一步学习建议

- **u5-l2 日志解析与源码行定位**：深入拆解 `ascendc_npuchk_report.py` 的 `parse_log` 与 `addr_to_line`，掌握手动用 `addr2line -f -e` 验证脚本输出的方法。
- **回头看 u3-l3**：若想理解 npuchk stub 的绑定细节（`dlsym`、`g_regStubs`、`IntriFmtGet`），复习 stub 注册机制。
- **动手扩展**：尝试在 add 样例里制造 `ErrorSync3`（set/wait 不配对）或 `ErrorWrite3`（重复写入），对照 `err_details` 字典验证报错，巩固错误体系。
- **延伸阅读**：`docs/01_cpu_debug.md`（gdb 调试）与 `docs/03_msobjdump.md`（离线 ELF 解析）能帮你把「CPU 调测 → npu check → 离线分析」的完整工具链补齐。
