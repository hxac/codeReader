# u9-l2 tiling_sink：设备侧 Tiling 下沉机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「把 Tiling 计算下沉到设备侧（AICPU）」到底解决什么问题：动态 shape 读不到、Host-Device 往返开销、super kernel 场景没有 Host 介入点。
2. 读懂 tiling_sink 的三件套源码：任务信封 `TilingAicpuTask`、注册表 `DeviceOpImplRegistry`、AICPU 执行器 `RunAicpuRpcSrvLaunch`，并能完整复述「下发任务 → 查注册表 → 执行 tiling → 回填 TilingData → 写 notify 标志」的执行链。
3. 在本仓库中识别该机制的「活体痕迹」：pioneer 算子的 `DeviceDoOpTiling*` 双入口导出、FAInfer 路线里的 `isTilingSink` 休眠分支。
4. 得出一个重要的工程判断力结论：tiling_sink 的框架代码随 common 目录携带，但在当前仓库快照中**不参与主构建**（构建文件可以证明），读公共组件必须用 CMake/grep 核实真实接线，不能只看目录名。

本讲是 u3-l3（tiling_base 责任链框架）的姊妹篇：tiling_base 解决「Host 侧由**哪个实现**做 tiling」，tiling_sink 解决「tiling **在哪里执行**」。

## 2. 前置知识

### 2.1 回顾：Host 侧 Tiling 做了什么

u2-l3 已经建立：Tiling 是 Kernel 启动前的 Host 侧「作战规划」，读入 shape 与平台信息，产出四项契约——`SetBlockDim`（核数）、`SetTilingKey`（分支信号）、序列化 TilingData（RawTilingData 字节流）、workspace 大小。整套流程的输入输出都封装在 `gert::TilingContext` 里。

本讲的关键问题就出在「Host 侧」三个字上：**如果 Tiling 需要的信息（比如每个 batch 的真实序列长度）只存在于设备侧张量里，Host 侧的 Tiling 代码怎么办？**

### 2.2 AICPU：设备上会跑 C++ 的「管家核」

u4-l9 已经介绍过：昇腾设备上除了做密集矩阵计算的 AICore，还有若干通用 CPU 核（AICPU，ARM 架构），跑的是普通 C++ 程序，擅长串行控制逻辑。这一点是 tiling_sink 的可行性基础——**Tiling 代码本质是 C++ 逻辑（读 shape、算切分、填结构体），不依赖任何 Host 独有的系统调用，因此同一份源码既能编进 Host 侧 optiling 库，也能编进设备侧 AICPU 库**。

### 2.3 super kernel：没有 Host 介入点的执行链

CANN 的 super kernel 机制把多个算子的 kernel 融合为一次下发，在设备上连续执行，中间**不再回到 Host**。此时链上后续算子的 Tiling 没有 Host CPU 可以代劳，只能请 AICPU 执行，算完后通过一个设备内存中的标志位通知等待方。本讲源码中 `notifyAddr` 的注释「super kernel 场景同步使用」（[tiling_sink_kernel.cpp:58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L58)）指的就是这个场景。（该机制的完整规范在 CANN 文档中，本仓库能看到的是执行器侧的实现。）

### 2.4 术语排雷：本仓库有三个「sink」

阅读时极易混淆，务必区分：

| 术语 | 含义 | 出现位置 |
|---|---|---|
| attention sink | 注意力的「沉洞 token」，前几拍 token 恒定参与打分 | FA 算子的 `sinkNum` 属性、UT 用例名 `FlashAttentionScoreEnhance_tiling_sinkNum2_*` |
| **tiling sink（本讲）** | 把 Tiling 计算下沉（sink）到设备侧执行 | common 的 tiling_sink 目录 |
| Sinkhorn | MHC 家族的双随机矩阵迭代算法（人名） | mhc 族算子 |

### 2.5 动态符号查找

u6-l2 讲过 torch 扩展用 `dlopen/dlsym` 按名解析 aclnn 符号。本讲会再次遇到同类手法：设备侧库用 `__attribute__((visibility("default")))` + `extern "C"` 把函数以**未改名、默认可见**的符号导出，供框架在运行期按名查找。这是跨模块（尤其是跨 Host/Device 边界）协作的通用套路。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用法 |
|---|---|---|
| `ascendc/src/ops-transformer/common/include/tiling_sink/tiling_aicpu_task.h` | 任务信封：AICPU 侧收到的参数结构体 | 模块 4.2 |
| `ascendc/src/ops-transformer/common/include/tiling_sink/device_op_impl_registry_impl.h` | 注册表的类声明 | 模块 4.3 |
| `ascendc/src/ops-transformer/common/src/tiling_sink/device_op_impl_registry.cpp` | 注册表的实现（单例 + map） | 模块 4.3 |
| `ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp` | AICPU 侧执行器入口 `RunAicpuRpcSrvLaunch` | 模块 4.4 |
| `ascendc/src/ops-transformer/common/include/tiling_sink/tiling_sink_kernel.h` | 执行器入口的对外声明 | 模块 4.1/4.4 |
| `ascendc/src/ops-transformer/common/src/tiling_sink/CMakeLists.txt` | opmaster 设备库的构建脚本（含休眠守卫） | 模块 4.5 |
| `ascendc/src/ops-transformer/common/CMakeLists.txt` | common 主构建脚本（证明 tiling_sink 不进主构建） | 模块 4.5 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp` | 活体样本：pioneer 的双入口导出 | 模块 4.5 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/flash_attention_infer_tiling.h` | 活体样本：`isTilingSink` 休眠分支 | 模块 4.5 |

注意：`register/device_op_impl_registry.h`（声明 `SinkTilingFunc`、`DeviceOpImplRegister` 与 `DEVICE_IMPL_OP_OPTILING` 宏）**不在本仓库**，它来自 CANN 包的 include 目录——这一点在 4.3 会再次强调。

## 4. 核心概念与源码讲解

### 4.1 动机与总体架构：为什么要把 Tiling 搬到设备上

#### 4.1.1 概念说明

Host 侧 Tiling 有三个盲区，每一个都真实出现在本仓库前几讲的算子里：

1. **动态 shape 读不到**。u4-l1 讲过 FA 前向是变长场景：`actual_seq_qlens` 等真实序列长度在 device 张量里。u4-l8 又讲过 pioneer 的 host tiling「仅收窄 TND/TND_NTD」，精确分核只能靠 metadata 张量在设备侧回填。根因相同——**Host 侧 tiling 代码拿不到 device 张量的内容**，要么依赖框架做一次 D2H 拷贝（阻塞异步流水），要么按 max 上界保守切分（核间负载不均）。
2. **Host-Device 往返开销**。Tiling 在 Host 算完，结果要随下发传回设备。decode 场景每步都是新 shape，每步都付一次往返。
3. **super kernel 没有 Host 介入点**。算子链在设备上连续执行时，后续算子的 Tiling 根本没有机会回到 Host。

tiling_sink 的答案：**既然 Tiling 是纯 C++ 逻辑，就把它编译到 AICPU 上执行**。AICPU 离 device 张量最近（可以直接读），离 AICore 最近（写个标志位就能同步），还不会打断 Host 的异步流水。

#### 4.1.2 核心流程

一帧使用 tiling_sink 的算子执行，宏观上是四步：

```text
① 框架把「给算子 X 做 tiling」打包成 TilingAicpuTask（信封）
② 任务被送到 AICPU，触发 RPC 服务入口 RunAicpuRpcSrvLaunch
③ 入口按 opType 查 DeviceOpImplRegistry 注册表，取出算子 X 的 SinkTilingFunc 并执行
④ tiling 函数把 TilingData/tilingKey/workspace 写回 TilingContext（回填），
   最后把 notifyAddr 置 1，通知等待方（AICore 侧）可以取参数启动 kernel 了
```

三个组件各司其职：信封（4.2）定义「送什么」、注册表（4.3）定义「找谁做」、执行器（4.4）定义「怎么做与怎么通知」。

#### 4.1.3 源码精读

总体架构的锚点是执行器入口的对外声明：

```cpp
extern "C" {
__attribute__((visibility("default"))) uint32_t RunAicpuRpcSrvLaunch(void *args);
}
```

[common/include/tiling_sink/tiling_sink_kernel.h:L20-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_sink/tiling_sink_kernel.h#L20-L22)：这是整个机制对外的唯一函数签名。`extern "C"` 保证符号名不被 C++ 名称修饰改写，`visibility("default")` 保证符号从动态库中导出（设备侧库默认 `-fvisibility=hidden`，见 4.5 的编译选项），`void *args` 是不透明任务指针——它的真身就是 4.2 的 `TilingAicpuTask`。

组件在仓库中的位置由 [ascendc/README.md:L111-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L111-L112) 的目录树标注：common 的 include（含 tiling_sink 公共头）与 src（含 tiling_sink 实现）。

#### 4.1.4 代码实践

**实践目标**：建立「同一份 tiling 代码可以编到两侧」的直觉。

**操作步骤**：

1. 打开 u2-l3 读过的 `ai_infra_aggregate_hidden_tiling.cpp`，观察它 include 的头文件（`exe_graph/runtime/tiling_context.h`、日志、平台信息）。
2. 思考：这些头里有没有 Host 独有的东西（如 `<iostream>` 控制台、文件 IO、fork/线程）？
3. 用下面的命令验证本仓库的 tiling 源文件是否只依赖「上下文传入的世界」：

```bash
grep -rln "iostream\|std::printf\|fopen" ascendc/src/ops-transformer --include="*_tiling.cpp"
```

**需要观察的现象**：tiling 实现全部通过 `gert::TilingContext` 参数获取输入、输出结果，几乎不直接触碰系统调用。

**预期结果**：绝大多数 tiling 文件不命中。这正是「可下沉」的代码特征：**依赖注入式的接口设计（一切经 context）是代码可移植到 AICPU 的前提**。若某个 tiling 实现里出现了 Host 独有调用，它就无法直接走 tiling_sink 路线。

#### 4.1.5 小练习与答案

**练习 1**：Host 侧 Tiling 想知道第 3 个 batch 的真实 KV 长度（值在 device 张量 `actual_seq_kvlen` 里），它有哪些办法？各自的代价是什么？

**答案**：(a) 让框架做 D2H 拷贝把张量内容搬到 Host——代价是同步阻塞，异步流水被打断；(b) 按 shape 声明的 max 上界保守切分——代价是核间负载不均、workspace 偏大；(c) 把 tiling 下沉到 AICPU 直接读——代价是引入本讲的整套机制、排查难度上升。本仓库 pioneer 算子实际选择的是第四条路：用 metadata AICPU 算子（u4-l9）在设备侧算好分核再由 kernel 回读，本质上是同一问题的「显式算子化」解法。

**练习 2**：为什么 `RunAicpuRpcSrvLaunch` 必须是 `extern "C"` 而普通 tiling 函数（如 `TilingAiInfraAggregateHidden`）不需要？

**答案**：普通 tiling 函数经 `IMPL_OP_OPTILING` 注册表按「函数指针」在编译期/链接期绑定，符号名无关紧要；而 `RunAicpuRpcSrvLaunch` 要被**另一个独立编译的模块**（AICPU 框架的 RPC 分发层）在运行期按名查找，`extern "C"` 保证符号名就是 `RunAicpuRpcSrvLaunch` 本身，不被 C++ 名称修饰（name mangling）改成带命名空间与参数类型的乱码。

---

### 4.2 任务信封：TilingAicpuTask

#### 4.2.1 概念说明

`TilingAicpuTask` 是「一次设备侧 tiling 请求」的参数包。Host/框架侧把要做 tiling 的算子类型、上下文指针、workspace 位置和同步地址打包进这个结构体，以不透明 `void*` 形式递给 AICPU。它是**两侧共同的协议**：一侧负责填充，`RunAicpuRpcSrvLaunch` 负责拆包。

#### 4.2.2 核心流程

```text
框架侧                          AICPU 侧
────────                        ────────
构造 TilingAicpuTask{
  tilingContext  ──────────────▶ reinterpret_cast 恢复为结构体指针
  opType         ──────────────▶ 用来查注册表（4.3）
  workspaceAddr/Size ──────────▶ 备用工作区
  notifyAddr     ──────────────▶ 完成后置 1，通知等待方（4.4）
}
```

注意 `tilingContext` 是指针：AICPU 侧拿到的 `gert::TilingContext*` 指向的是**框架在设备内存中重建的上下文对象**（如何重建属于 CANN 框架层，不在本仓库），本仓库代码只负责消费它。

#### 4.2.3 源码精读

```cpp
namespace tilingsink {
struct TilingAicpuTask {
  gert::TilingContext *tilingContext;
  const char *opType;
  uint64_t notifyAddr;
  uint64_t workspaceAddr;
  uint64_t workspaceSize;
};
}  // namespace optiling
```

[common/include/tiling_sink/tiling_aicpu_task.h:L20-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_sink/tiling_aicpu_task.h#L20-L28)：定义任务信封。五个字段的职责分别是：

| 字段 | 类型 | 职责 |
|---|---|---|
| `tilingContext` | `gert::TilingContext*` | 被重建的 tiling 上下文；tiling 函数读输入、回填 TilingData 全靠它 |
| `opType` | `const char*` | 算子类型字符串，注册表的查询键 |
| `notifyAddr` | `uint64_t` | 完成通知地址；tiling 成功后被置 1 |
| `workspaceAddr` | `uint64_t` | 本次 tiling 可用的 workspace 起址 |
| `workspaceSize` | `uint64_t` | workspace 大小 |

两个值得玩味的细节：其一，第 18 行 include 的是 `exe_graph/runtime/tiling_context.h`——信封与普通 Host tiling 共用同一个上下文类型，这是「同一份 tiling 代码两侧可编」在类型系统上的落实；其二，命名空间实际是 `tilingsink`，但收尾注释写的是 `// namespace optiling`（第 28 行）——从 optiling 体系拷贝代码时留下的痕迹，阅读时以实际 `namespace tilingsink` 为准（4.4 中执行器正是用 `tilingsink::TilingAicpuTask` 引用它）。

#### 4.2.4 代码实践

**实践目标**：确认 `TilingAicpuTask` 在仓库中的生产/消费关系，体会「协议结构体」的窄接口特征。

**操作步骤**：

```bash
grep -rn "TilingAicpuTask" ascendc/src/ --include=*.h --include=*.cpp
```

**需要观察的现象**：命中只有两处——定义（tiling_aicpu_task.h）与消费（tiling_sink_kernel.cpp 第 33 行的 `reinterpret_cast`）。

**预期结果**：仓库内**没有任何代码构造这个结构体**。填充方在 CANN 框架层（不在本仓库）。这符合协议结构体的设计：本仓库只定义「长什么样」并负责「怎么消费」，生产者由框架实现。与 u3-l3 中 `tiling_util`「无调用者」不同，这里的单向引用是**协议角色使然**，而非死代码。

#### 4.2.5 小练习与答案

**练习 1**：`opType` 为什么用 `const char*` 而不是 `std::string`？

**答案**：这是跨越「不透明 `void*` 边界」的 POD 风格结构体。生产方（框架）与消费方（AICPU 上加载的本仓库代码）可能由不同编译单元、不同构建产出，保持 trivially-copyable 的 C 类型能避免 ABI 风险（std::string 的内部布局并非跨模块稳定契约）。执行器拿到后自己转 `std::string` 再查表（见 4.4 第 ② 步）。

**练习 2**：`notifyAddr` 与 `workspaceAddr` 为什么分开两个字段，而不是把 notify 标志放进 workspace？

**答案**：语义与生命周期不同。workspace 是**数据缓冲**（tiling 过程的暂存），notify 是**同步信号**（跨核可见性敏感，写完还要 `dsb st` 屏障）。分开后 workspace 可以复用/重分配而不影响同步协议；notify 地址则由框架统一管理，等待方只需要盯住一个与数据无关的地址。

---

### 4.3 注册表：DeviceOpImplRegistry 的注册与查找

#### 4.3.1 概念说明

AICPU 执行器收到任务后只认识 `opType` 字符串，不认识任何具体算子。需要一个「算子名 → tiling 函数」的查找表——`DeviceOpImplRegistry`。它与 u3-l3 的 `tiling_templates_registry` 形神皆似（单例 + 注册宏 + 工厂查找），但服务对象不同：那边组织 Host 侧的多个候选实现，这边只做**一算子一函数**的设备侧入口绑定。

再次强调：基类声明与 `DEVICE_IMPL_OP_OPTILING` 宏来自 CANN 包头 `register/device_op_impl_registry.h`（本仓库 grep 不到该文件），本仓库提供的是实现。

#### 4.3.2 核心流程

注册（构建产物加载时执行一次）：

```text
DEVICE_IMPL_OP_OPTILING(OpType)          ← 宏，展开出一个 DeviceOpImplRegister 对象并记下算子名
        │
        ▼
DeviceOpImplRegister(opType)             ← 构造：new 出 impl_，保存 opType 字符串
        │
        ▼
.Tiling(func)                            ← 流式调用：把 (opType, func) 写进单例注册表
        │
        ▼
DeviceOpImplRegistry::RegisterSinkTiling(opType, func)
        sinkTilingFuncsMap_[opType] = func
```

查找（每次任务到达时执行）：

```text
GetSinkTilingFunc(opType)
        │
        ├── map::find 命中 → 返回 func
        └── 未命中        → 返回 nullptr（执行器据此报「算子未注册」错误）
```

#### 4.3.3 源码精读

先看类声明：

```cpp
class DeviceOpImplRegistry {
public:
  static DeviceOpImplRegistry& GetSingleton();
  void RegisterSinkTiling(std::string &opType, SinkTilingFunc& func);
  SinkTilingFunc GetSinkTilingFunc(std::string &opType);
private:
  std::map<std::string, SinkTilingFunc> sinkTilingFuncsMap_;
};

class DeviceOpImplRegisterImpl {
public:
  std::string& GetOpType();
private:
  std::string opType_ = "";
};
```

[common/include/tiling_sink/device_op_impl_registry_impl.h:L24-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/include/tiling_sink/device_op_impl_registry_impl.h#L24-L47)：左边是注册表本体（单例 + 一个 map 成员），右边是注册器对象的私有实现类（pimpl 手法，只存算子名）。`SinkTilingFunc` 是可比较 nullptr、可直接调用的函数类型（从 4.4 的用法 `func == nullptr`、`(func)(ctx)` 可证），具体 typedef 在 CANN 包头中。

再看实现：

```cpp
DeviceOpImplRegistry &DeviceOpImplRegistry::GetSingleton()
{
  static DeviceOpImplRegistry g_deviceOpImplRegistry;
  return g_deviceOpImplRegistry;
}

void DeviceOpImplRegistry::RegisterSinkTiling(std::string &opType, SinkTilingFunc &func)
{
  std::string opTypeString = opType;
  sinkTilingFuncsMap_[opTypeString] = func;
}

SinkTilingFunc DeviceOpImplRegistry::GetSinkTilingFunc(std::string &opType)
{
  std::string opTypeString = opType;
  auto func = sinkTilingFuncsMap_.find(opTypeString);
  if (func == sinkTilingFuncsMap_.end()) {
    return nullptr;
  }
  return func->second;
}
```

[common/src/tiling_sink/device_op_impl_registry.cpp:L21-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/device_op_impl_registry.cpp#L21-L41)：单例用 C++11 magic static（首次调用时构造、线程安全）；注册就是 `map[key]=func`——**同名重复注册是静默覆盖**，后注册者胜出；查找未命中返回 `nullptr`，把「未注册」交给调用方裁决。

流式注册器：

```cpp
DeviceOpImplRegister::DeviceOpImplRegister(const char *opType)
{
  impl_ = std::make_unique<DeviceOpImplRegisterImpl>();
  impl_->GetOpType() = opType;
}

DeviceOpImplRegister &DeviceOpImplRegister::Tiling(SinkTilingFunc func)
{
  DeviceOpImplRegistry::GetSingleton().RegisterSinkTiling(impl_->GetOpType(), func);
  return *this;
}
```

[common/src/tiling_sink/device_op_impl_registry.cpp:L52-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/device_op_impl_registry.cpp#L52-L62)：构造函数记下算子名，`.Tiling(func)` 完成注册并返回自身引用以支持链式调用（`DEVICE_IMPL_OP_OPTILING(X).Tiling(f)` 一行完成注册）。紧随其后的拷贝/移动构造（[L64-L74](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/device_op_impl_registry.cpp#L64-L74)）都只复制 opType、不复制已注册状态——拷贝出一个注册器不会引发重复注册。

#### 4.3.4 代码实践

**实践目标**：亲手跑通「注册 → 查找 → 执行」闭环，理解注册表不过是一个 map 加一层单例。

**操作步骤**：下面的「示例代码」把注册表核心逻辑抽成一个不依赖任何 CANN 头的自包含程序（任何一个有 g++ 的 Linux 宿主机都能跑，无需 NPU）：

```cpp
// registry_replica.cpp —— 示例代码：DeviceOpImplRegistry 的最小复刻
#include <map>
#include <string>
#include <functional>
#include <iostream>
#include <cstdint>

using SinkTilingFunc = uint32_t (*)(void *);   // 对应 SinkTilingFunc

class RegistryReplica {                         // 对应 DeviceOpImplRegistry
public:
    static RegistryReplica &GetSingleton() {
        static RegistryReplica inst;            // magic static 单例
        return inst;
    }
    void Register(const std::string &op, SinkTilingFunc f) { table_[op] = f; }
    SinkTilingFunc Lookup(const std::string &op) {
        auto it = table_.find(op);
        return it == table_.end() ? nullptr : it->second;
    }
private:
    std::map<std::string, SinkTilingFunc> table_;
};

struct Registrar {                              // 对应 DeviceOpImplRegister 的链式注册
    Registrar(const char *op) : op_(op) {}
    Registrar &Tiling(SinkTilingFunc f) {
        RegistryReplica::GetSingleton().Register(op_, f);
        return *this;
    }
    std::string op_;
};

uint32_t MyOpTiling(void *ctx) {                // 一个假想的 sink tiling 实现
    std::cout << "tiling for ctx=" << ctx << std::endl;
    return 0;                                   // 0 对应 KERNEL_STATUS_OK
}

int main() {
    Registrar("MyOp").Tiling(&MyOpTiling);      // 等价于 DEVICE_IMPL_OP_OPTILING(MyOp).Tiling(...)
    auto f = RegistryReplica::GetSingleton().Lookup("MyOp");
    std::cout << "found: " << (f != nullptr) << std::endl;
    f(nullptr);                                 // 命中并执行
    auto g = RegistryReplica::GetSingleton().Lookup("NotRegistered");
    std::cout << "not registered: " << (g == nullptr) << std::endl;
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 registry_replica.cpp -o registry_replica && ./registry_replica
```

**需要观察的现象**：输出 `found: 1`、`tiling for ctx=0`、`not registered: 1`。

**预期结果**：注册一次即可按名命中；未注册的名字拿到 `nullptr`——这正是 4.4 执行器里 `func == nullptr` 报错分支的来由。再把 `Registrar("MyOp").Tiling(...)` 复制一行改成别的函数，可验证「同名重复注册静默覆盖」。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `std::map` 而不是 `std::unordered_map`？查找性能敏感吗？

**答案**：查找只在每次设备侧 tiling 任务到达时发生一次，每次 tiling 本身要做大量 shape 运算与内存读写，map 与 unordered_map 的查找差异（对数级 vs 常数级）在这里完全不重要；map 还有键有序、迭代器稳定、 ABI 更保守的好处。这是「注册表类组件」的常见选型：**注册/查找频率都极低，简单稳妥优先**。

**练习 2**：如果两个动态库都注册了 `"FusedInferAttentionScore"`，会发生什么？

**答案**：二者共用同一个进程内单例（magic static 是进程级唯一），后加载者的注册会覆盖前者（`map[key]=func` 赋值语义）。这提示设备侧库的算子名必须全局协调——与 u1-l2 讲过的「四层靠算子名对齐」一脉相承：**算子名是跨层、跨库、跨 Host/Device 的全局主键**。

---

### 4.4 AICPU 执行器：RunAicpuRpcSrvLaunch

#### 4.4.1 概念说明

`RunAicpuRpcSrvLaunch` 是 AICPU 上的 RPC 服务入口：框架把 `TilingAicpuTask` 送到时，这个函数被调用。它把 4.2 的信封、4.3 的注册表串成完整流程，并在结尾完成跨核同步（写 notify 标志）。它是本机制中**唯一在设备侧执行**的仓库代码，也是理解「tiling 如何在设备上落地」的核心。

#### 4.4.2 核心流程

八步流水（对应下方源码标注）：

```text
① 空参防御        args == nullptr → 参数错误返回
② 拆信封          args → TilingAicpuTask*；opType 空指针 → 参数错误返回
③ 查注册表        GetSingleton().GetSinkTilingFunc(opType)
④ 未注册防御      func == nullptr → 参数错误返回（带算子名日志）
⑤ 上下文防御      tilingContext 为空 → 参数错误返回
⑥ 执行 tiling     func(tilingContext)，返回非 GRAPH_SUCCESS → 内部错误返回
                  └─ tiling 函数内部回填 TilingData/tilingKey/blockDim/workspace
⑦ 写完成通知      *notifyAddr = 1；aarch64 上追加 dsb st 屏障
⑧ 返回 OK
```

#### 4.4.3 源码精读

主体骨架与防御式校验（延续 u3-l1 的 `OP_CHECK_IF`/`OP_LOGE` 风格，这里写成显式 if）：

```cpp
__attribute__((visibility("default"))) uint32_t RunAicpuRpcSrvLaunch(void *args)
{
#ifndef ASCEND_OPTILING_UT
  ...
  tilingsink::TilingAicpuTask *task = reinterpret_cast<tilingsink::TilingAicpuTask*>(args);
  std::string opType = task->opType;
  optiling::SinkTilingFunc func =
    optiling::DeviceOpImplRegistry::GetSingleton().GetSinkTilingFunc(opType);
  if (func == nullptr) { OP_LOGE(opType.c_str(), "func is nullptr, check if op is registered"); ... }
  if (!task->tilingContext) { ... }
  if ((func)(task->tilingContext) != ge::GRAPH_SUCCESS) { ... }
```

[common/src/tiling_sink/tiling_sink_kernel.cpp:L25-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L25-L53)：AICPU 执行器主体。注意三个细节：错误码用 `aicpu::KERNEL_STATUS_PARAM_INVALID`/`KERNEL_STATUS_INNER_ERROR`/`KERNEL_STATUS_OK`（AICPU 内核状态码，而非 ge::GRAPH_*，因为这是内核态服务的返回契约）；`opType` 被转成 `std::string` 后既做查表键又做日志前缀，日志能精确定位是哪个算子的 tiling 失败；整个函数体被 `#ifndef ASCEND_OPTILING_UT` 包住——UT 构建下它退化为直接 `return KERNEL_STATUS_OK` 的空壳，与 u3-l4 的桩哲学同源：**被测代码所在的环境不可用时，用编译开关把边界掏空**。

跨核同步段：

```cpp
  if (reinterpret_cast<uint64_t *>(task->notifyAddr) != nullptr) {
    *reinterpret_cast<uint64_t *>(task->notifyAddr) = 1; // 将此地址置1，super kernel场景同步使用
    #ifdef __aarch64__
      // 插入一句汇编，作用是等待所有存储操作及相关缓存和缓冲区维护操作完成，
      // 这里是保证写地址notifyAddr的操作完成
      __asm__ __volatile__("dsb st" : : : "memory");
    #endif
  }
```

[common/src/tiling_sink/tiling_sink_kernel.cpp:L56-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L56-L64)：tiling 成功后把 notify 地址写成 1，等待方（AICore 上的 super kernel，由注释指明）轮询到 1 即知 TilingData 就绪。`dsb st`（Data Synchronization Barrier, Store）是 ARM 的存储屏障指令，保证「置 1」真正到达内存可见层级后才继续——否则等待核可能看到过期的旧值。`__aarch64__` 宏限定只在 ARM 64 位设备编译路径插入（AICPU 是 ARM 核；x86 宿主机 UT 编译没有这条指令）。

文件尾部的注册：

```cpp
DEVICE_IMPL_OP_OPTILING(FusedInferAttentionScore).Tiling(optiling::DeviceDoOpTilingFusedInferAttentionScore);
DEVICE_IMPL_OP_OPTILING(IncreFlashAttention).Tiling(optiling::DeviceDoOpTilingIncreFlashAttention);
```

[common/src/tiling_sink/tiling_sink_kernel.cpp:L72-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L72-L75)：用 4.3 的机制注册两个设备侧 tiling 实现。`DeviceDoOpTilingFusedInferAttentionScore` 等符号来自 include 的 `fused_infer_attention_score_tiling.h`（第 22 行）——**这些头文件与对应算子目录并不在本仓库**（见 4.6 的构建分析），它们属于 CANN 内置推理算子工程，此文件是随上游公共代码一并携带的「原厂接线」。

#### 4.4.4 代码实践

**实践目标**：以只读方式走通执行器的每一条防御分支，理解「错误码三段式」的返回值设计。

**操作步骤**：

1. 通读 [tiling_sink_kernel.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L25-L69) 全文，为 ①~⑧ 每一步在源码中标注行号。
2. 列出所有提前 return 点及其返回值，填入下表（示例已给一行）：

| 分支 | 返回值 | 含义 |
|---|---|---|
| args 为空 | `KERNEL_STATUS_PARAM_INVALID` | 任务指针不合法 |
| opType 为空 | （待你填写） | |
| 算子未注册 | （待你填写） | |
| tilingContext 为空 | （待你填写） | |
| tiling 执行失败 | （待你填写） | |
| 全部成功 | `KERNEL_STATUS_OK` | |

3. 思考题写在笔记里：如果把 `dsb st` 那一行删掉，什么样的时序下等待方会出错？

**需要观察的现象**：所有参数错误都归并为同一个 `KERNEL_STATUS_PARAM_INVALID`，内部执行错误单独用 `KERNEL_STATUS_INNER_ERROR`——错误粒度是「谁的锅」（调用方传参 vs 实现自身），细节差异靠 OP_LOGE 日志区分。

**预期结果**：六个返回点如上表；`dsb st` 删除后，在 AICPU 与 AICore 各自有写缓冲/缓存的弱序内存模型下，「写 notify=1」可能晚于后续指令对外可见，等待核轮询到旧值 0 会误判「tiling 未完成」，造成 super kernel 偶发挂起——且极难复现排查。这正是内存屏障存在的意义（结论为原理推导，待本地验证指真实设备上的可复现实验）。

#### 4.4.5 小练习与答案

**练习 1**：执行器为什么不像 u3-l1 推崇的那样统一用 `OP_CHECK_IF` 宏，而是手写 if？

**答案**：二者效果等价（`OP_CHECK_IF` 本就是「条件+日志+return」三段式的宏包装）。手写 if 让每个分支返回**不同的 AICPU 内核状态码**成为自然写法；`OP_CHECK_IF` 的 return 语句由宏注入，适合统一返回 `ge::GRAPH_FAILED` 的 Host 侧 tiling 场景。工具服从于返回值契约。

**练习 2**：`reinterpret_cast<uint64_t *>(task->notifyAddr) != nullptr` 这个判空写法有什么味道？等价的更直白写法是什么？

**答案**：它先把整型地址转成指针再判空，等价于 `task->notifyAddr != 0`。功能正确但绕了一层；不过它同时保证了「后面两个 `reinterpret_cast` 表达式里使用的指针类型」与判空对象类型一致，属于防御式冗余。读仓库代码时经常遇到这类风格，识别意图（判「有没有通知地址」）比纠结写法更重要。

---

### 4.5 活体样本与构建现实：pioneer 双入口与休眠的 opmaster

#### 4.5.1 概念说明

前四个模块讲的是 tiling_sink 的「通用框架」。本模块回答两个关键问题：**这套机制在本仓库有活的用法吗？它参与编译吗？** 结论先行：

- 活体痕迹一：pioneer 算子把**同一份 tiling 实现**同时暴露为 Host 入口（`IMPL_OP_OPTILING` 注册）与设备可查入口（`DeviceDoOpTiling*` 导出符号）——「一套实现、两个入口」，正是 tiling_sink 需要的代码形态。
- 活体痕迹二：pioneer 的 FAInfer（CUTLASS 风格备选路线，见 u4-l8）tiling 里留有 `isTilingSink` 分支，设备侧分核与 workspace 语义随之变化。
- 构建现实：`tiling_sink` 目录**不进主构建**，其独立 CMake 工程因依赖的上游算子源码不在本仓库而提前 return——框架代码「随仓携带、当前休眠」。

#### 4.5.2 核心流程

pioneer 的双入口结构：

```text
ai_infra_attention_pioneer_tiling_register.cpp    ai_infra_attention_pioneer_tiling.cpp
────────────────────────────                      ────────────────────────────────────
IMPL_OP_OPTILING(AiInfraAttentionPioneer)
    .Tiling(DoOpTilingAiInfraAttentionPioneer) ◀── Host 入口：注册进 Host 侧 tiling 注册表
                                                   │  DoOpTiling... (namespace 内符号)
                                                   │    └─ TilingAiInfraAttentionPioneerV2(ctx)  ← 同一实现
                                                   ▼
                                       extern "C" + visibility("default")
                                       DeviceDoOpTilingAiInfraAttentionPioneer ◀── 设备入口：
                                                          按名可查的导出符号，包一层调用同一实现
```

FAInfer 路线的 `isTilingSink` 分支语义：

```text
DoTiling:
  FillBasicTilingData          ← 两种模式都做（基础参数总是 Host 可得）
  if (!isTilingSink):          ← Host tiling 模式：
      FillSplitCoreTilingData      做核间切分（此时知道全部信息）
  FillWorkSpaceTilingData:
      if (isTilingSink):       ← 下沉模式：切分留给设备侧做，
          splitLse/splitO 尺寸 ×2    需要双倍 split 缓存（两阶段/双缓冲），
          set_needCoreNum           并把核数写进 TilingData 供设备侧使用
      else:                        Host 模式：直接读回已算好的切分结果
```

#### 4.5.3 源码精读

pioneer 的两个入口：

```cpp
AP_EXTERN_C ge::graphStatus DoOpTilingAiInfraAttentionPioneer(gert::TilingContext *context)
{
    ...
    return TilingAiInfraAttentionPioneerV2(context);
}

extern "C" {
__attribute__((visibility("default"))) ge::graphStatus DeviceDoOpTilingAiInfraAttentionPioneer(
    gert::TilingContext *context)
{
    return DoOpTilingAiInfraAttentionPioneer(context);
}
}
```

[ai_infra_attention_pioneer_tiling.cpp:L27-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp#L27-L41)：`DoOpTiling...` 是 namespace 内的普通实现（转调 arch35 的 `TilingAiInfraAttentionPioneerV2`）；`DeviceDoOpTiling...` 只是给它套了一层 `extern "C"` + 默认可见性的**别名壳**。两份声明见 [ai_infra_attention_pioneer_tiling.h:L205-L208](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.h#L205-L208)。Host 侧注册在 [ai_infra_attention_pioneer_tiling_register.cpp:L26-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling_register.cpp#L26-L29)：`IMPL_OP_OPTILING(AiInfraAttentionPioneer).Tiling(DoOpTilingAiInfraAttentionPioneer)`——与 u2-l2/u2-l3 讲过的注册方式完全一致。也就是说：**这份 tiling 代码现在由 Host 注册表驱动执行，同时预留了被设备侧按名调用的形态**。

FAInfer 的休眠分支：

```cpp
ge::graphStatus FAInferTiling::DoTiling(FAInferTilingData &tilingdata)
{
    FillBasicTilingData(tilingdata);
    if (!faInfo_.isTilingSink) {
        FillSplitCoreTilingData(tilingdata);
        if (faInfo_.flashDecodeFlag) {
            splitBN2S1GS2(tilingdata);
        }
    }
    FillWorkSpaceTilingData(tilingdata);
    return ge::GRAPH_SUCCESS;
}
```

[flash_attention_infer_tiling.h:L655-L666](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/flash_attention_infer_tiling.h#L655-L666)：`isTilingSink` 为真时跳过核间切分——因为下沉模式下切分所需的精确信息（真实序列长度）此时还在设备上，切分逻辑要等设备侧执行时才能做。workspace 侧的配套变化在 [L286-L297](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/flash_attention_infer_tiling.h#L286-L297)：下沉模式下把 `splitLseTotalSize`/`splitOTotalSize` 按 `2 × blockNum × ...` 计算并连同 `needCoreNum` 写进 TilingData（split 中间结果需要两份缓存），Host 模式则直接读回前一步已算好的值。标志声明在 `FAInferContext` 的 [L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/flash_attention_infer_tiling.h#L141)（默认 false）。同族还有 arch35 tiling context 里的 `fromTilingSink` 字段（[ai_infra_attention_pioneer_tiling_context.h:L131-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_context.h#L131-L132)，注释说明它是「从 tiling 下沉进入 workspace 计算步骤」的标志）——但全仓库 grep 仅有此一处声明，未被赋值。**当前快照里这些分支均处于休眠状态**，是上游能力在本仓库的预留接口。

构建现实的两份证据：

```cmake
file(GLOB CPP_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/src/tiling_base/*.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")
```

[common/CMakeLists.txt:L55-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L55-L56)：common 主构建只收集 `src/tiling_base/*.cpp` 与 `src/*.cpp`（另一分支 [L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/CMakeLists.txt#L87) 同样），**`src/tiling_sink/` 不在收集范围**——tiling_sink 的两个 .cpp 不会编进任何主目标。

```cmake
foreach(f ${src_files})
  if(NOT EXISTS ${f})
    message(WARNING "File not found: ${f}")
    return()
  endif
endforeach()

add_library(opmaster SHARED ${src_files})
```

[common/src/tiling_sink/CMakeLists.txt:L40-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/CMakeLists.txt#L40-L49)：tiling_sink 是个独立工程（自带 `project(tiling_sink_project)`），产物为设备侧动态库 `opmaster`。但 `src_files`（[L16-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/CMakeLists.txt#L16-L38)）引用了 `attention/fused_infer_attention_score`、`attention/prompt_flash_attention`、`attention/incre_flash_attention` 及 `attention/common/op_host/fia_tiling_*.cpp` 等一批**本仓库不存在的上游文件**；foreach 守卫发现缺文件即 `return()`，整个工程静默退出。此外该工程链接 AICPU 侧库（`intf_pub_aicpu`、`exe_meta_device`，见 [L115-L127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/CMakeLists.txt#L115-L127)）并定义 `DEVICE_OP_TILING_LIB`、`BUILT_IN_TILING_SINK` 编译宏（[L138-L143](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/CMakeLists.txt#L138-L143)），进一步印证 opmaster 是给 AICPU 环境（内置算子工程）准备的产物。

#### 4.5.4 代码实践

**实践目标**：用只读命令证明「休眠」结论，锻炼「以构建文件为准」的核实能力（承接 u3-l3 对 tiling_util 的核查方法）。

**操作步骤**：

```bash
# 1. 确认 opmaster 依赖的上游目录不存在
ls ascendc/src/ops-transformer/attention/
ls ascendc/src/ops-transformer/attention/common/op_host/

# 2. 确认没有任何父工程挂接 tiling_sink
grep -rn "tiling_sink" ascendc --include=CMakeLists.txt --include=*.cmake

# 3. 确认 pioneer 设备入口符号的真实存在与导出意图
grep -rn "DeviceDoOpTiling" ascendc/src/ops-transformer --include=*.cpp --include=*.h
```

**需要观察的现象**：第 1 步的 attention 目录列表里没有 `fused_infer_attention_score`/`prompt_flash_attention`/`incre_flash_attention`，`common/op_host` 里也没有 `fia_tiling_info.cpp` 等；第 2 步只命中 tiling_sink 自己目录内的三行；第 3 步命中 pioneer 的 tiling.cpp/h 各一处。

**预期结果**：三条证据共同支撑结论——tiling_sink 框架代码在本仓库**不参与任何构建**（无人 add_subdirectory，独立工程自身也会因缺文件提前 return）；它服务的对象（CANN 内置推理算子）源码不在本仓库；而 pioneer 的 `DeviceDoOpTiling*` 导出是随算子自身源码编进 optiling 库的，不依赖 opmaster。若未来上游把内置算子源码带入，这套框架即可被重新激活。

#### 4.5.5 小练习与答案

**练习 1**：pioneer 为什么不像 tiling_sink_kernel.cpp 那样用 `DEVICE_IMPL_OP_OPTILING` 宏注册设备入口，而是自己导出 `DeviceDoOpTiling*` 符号？

**答案**：两条路线的「查找方」不同。`DEVICE_IMPL_OP_OPTILING` 注册进 `DeviceOpImplRegistry`，服务于「AICPU 执行器按 opType 字符串查表」的框架路线（依赖 opmaster 库被加载）；而 `DeviceDoOpTiling*` 是按符号名直接导出，服务于「框架按命名约定 dlsym 查找」的路线，随算子自己的 optiling 产物分发、不需要额外注册表。 pioneer 选了后者，使其设备侧入口不依赖 opmaster 是否存在。两条路线在本仓库各留一份样本，恰好构成对照。

**练习 2**：`isTilingSink` 分支里，为什么下沉模式反而**多**算了 splitLse/splitO 两块 workspace？

**答案**：下沉模式把核间切分推迟到设备侧执行，切分中间产物（各核的 LSE 部分和、输出部分和）需要在 workspace 里落地再合并，双缓冲（×2）让生产与消费重叠；Host 模式下切分在 tiling 阶段一次算完、直接写进 TilingData 数组，不需要这两块动态缓存。**把计算搬到执行更晚的阶段，往往要把状态从寄存器/结构体搬进显存**——这是空间换时序自由的典型交换。

**练习 3**：如果让你把 aggregate_hidden（u2-l3 的标本算子）改造成支持 tiling_sink，最少要做哪几件事？

**答案**：① 给它的 tiling 实现套一层 `extern "C" __attribute__((visibility("default"))) DeviceDoOpTiling...` 别名壳（照抄 pioneer 模式，改动最小）；② 审查 tiling 实现是否有 Host 独有依赖，确保一切输入经 `gert::TilingContext`（aggregate_hidden 已满足）；③ 若走 opmaster 注册路线，还需在 tiling_sink_kernel.cpp 末尾追加一行 `DEVICE_IMPL_OP_OPTILING(...)` 注册——但该库当前不构建，实际应选 dlsym 路线。核心认识：**机制的全部复杂度在框架侧，算子侧适配成本很低**。

## 5. 综合实践

本讲综合实践就是规格中布置的两项产出。

### 5.1 画出一帧使用 tiling_sink 的算子执行时序

请基于 4.2~4.4 的源码，独立画出时序图后再与下面的参考图对照（以 super kernel 链中的变长注意力算子为例）：

```text
Host CPU                     AICPU（设备）                      AICore（设备）
────────                     ─────────────                      ─────────────
下发 super kernel 链
（首个 kernel 启动后 Host 即离场）
                             ┌──────────────────────────┐
   ①框架构造 TilingAicpuTask │ RunAicpuRpcSrvLaunch:    │
   {tilingContext,opType, ──▶│ ②args→task* 拆信封        │      前一个 kernel 仍在执行
    notifyAddr,workspace}    │ ③opType→registry 查表     │      （流水不被打断）
                             │   └ 未注册→报错返回 ✗     │
                             │ ④func(tilingContext)      │
                             │   ├ 读 device 上的 seq 张量│
                             │   ├ 按真实长度切分核间任务 │
                             │   ├ SetTilingData/TilingKey│
                             │   └ workspace 需求回填 ctx │
                             │ ⑤*notifyAddr=1; dsb st ──────────▶ 轮询 notifyAddr==1
                             └──────────────────────────┘      ⑥从约定地址取 TilingData
                             return KERNEL_STATUS_OK            ⑦按 blockDim 启动本算子 kernel
```

自检要点（图中每个编号都应能在源码中指出行号）：② 对应 [tiling_sink_kernel.cpp:L33-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L33-L37)，③ 对应 [L38-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L38-L44)（查表在 [device_op_impl_registry.cpp:L33-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/device_op_impl_registry.cpp#L33-L41)），④ 对应 [L50-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L50-L53)，⑤ 对应 [L56-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/common/src/tiling_sink/tiling_sink_kernel.cpp#L56-L64)；⑥⑦ 由框架与 kernel 侧约定完成（本仓库不含该侧代码，属于协议消费端）。

### 5.2 Host tiling 与 device tiling 的动态 shape 对比与选型建议

先自行填写对比表，再对照参考：

| 维度 | Host 侧 Tiling（u2-l3 主线） | 设备侧 Tiling（tiling_sink） |
|---|---|---|
| 动态 shape 信息获取 | 只能用静态 shape 与属性；device 张量内容需 D2H 拷贝或按 max 上界保守估计 | AICPU 直接读 device 张量，拿到真实长度 |
| 切分质量 | 按上界切 → 短序列时核间负载严重不均 | 按真实长度切 → 均衡（pioneer metadata 的贪心分核同此理） |
| 流水影响 | D2H 拷贝与往返打断 Host 异步流水；decode 每步都付 | 与 AICore 执行重叠，Host 全程离场 |
| super kernel 场景 | 不可用（无 Host 介入点） | 可用（notifyAddr + dsb st 同步） |
| workspace 语义 | Host 一次算准 | 须为设备侧切分预留 split 双缓冲（见 4.5 的 ×2） |
| 可调试与可测试 | Host 日志直达；u8 的 tiling UT 框架直接覆盖 | AICPU 上排查困难；UT 需 `ASCEND_OPTILING_UT` 掏空 |
| 工程成本 | 全仓库默认路径，零额外设施 | 需执行器/注册表/任务协议整套设施，或至少 dlsym 导出入口 |

**选型建议（参考范文）**：静态 shape、离线编译可缓存切分的算子（如本仓库的 aggregate_hidden、sinkhorn），Host tiling 是唯一合理选择——设施简单、UT 体系现成。动态 shape 且序列长度已在 device 张量上的算子（变长 FA、变长 decode），优先考虑「避免 Host 知晓真实长度」的两条设备侧路线：若只需精确分核，显式 AICPU 元数据算子（pioneer metadata，u4-l9）侵入小、行为直观、可单测；若处于 super kernel 长链或追求 tiling 与执行的完全流水，则用 tiling_sink 式下沉，但要接受 workspace 预留变大、调试面复杂、以及在本仓库当前快照下相关基础设施尚未接入主构建的现实。一句话：**按「shape 信息住在哪里」决定 tiling 住在哪里**；Host 拿得到就不必下沉，Host 拿不到就按链路形态在「显式元数据算子」与「框架级下沉」之间二选一。

## 6. 本讲小结

- tiling_sink 把 Tiling 计算从 Host CPU 下沉到设备上的 AICPU 执行，动机是三个 Host 盲区：动态 shape 读不到、Host-Device 往返开销、super kernel 场景没有 Host 介入点；可行性基础是 Tiling 为纯 C++ 逻辑、且经 `gert::TilingContext` 依赖注入。
- 三件套分工：`TilingAicpuTask`（4.2）定义任务信封五字段；`DeviceOpImplRegistry`（4.3）单例 map 完成「算子名 → sink tiling 函数」的注册与查找（同名重复注册静默覆盖）；`RunAicpuRpcSrvLaunch`（4.4）在 AICPU 上拆信封、查表、执行 tiling、回填 TilingData，最后写 `notifyAddr=1` 并用 `dsb st` 保证跨核可见。
- 活体样本：pioneer 以「一套实现、两个入口」（`IMPL_OP_OPTILING` Host 注册 + `DeviceDoOpTiling*` 默认可见导出）预留设备侧调用形态；FAInfer 路线的 `isTilingSink` 分支展示下沉模式的典型差异——核间切分推迟到设备侧、workspace 需 ×2 split 双缓冲。
- 构建现实：`common/CMakeLists.txt` 只 glob `src/tiling_base`，tiling_sink 独立工程又因上游内置算子源码缺失而提前 return——框架代码随仓携带但当前休眠。**读公共组件必须以构建文件和 grep 为准，不能只看目录名**（与 u3-l3 的 tiling_util 教训互证）。
- 术语排雷：attention sink（沉洞 token）、tiling sink（本讲的 tiling 下沉）、Sinkhorn（MHC 算法）三者无关。

## 7. 下一步学习建议

- 下一讲 u9-l3 转向 common 的 **fallback 机制**——算子输入不满足约束时的两级降级策略，与 tiling_sink 同属「让算子在边界条件下仍能完成任务」的公共设施，但方向相反：一个是把计算换个地方执行，一个是把任务换个方式完成。
- 想继续深挖设备侧执行：对照阅读 u4-l9 的 metadata AICPU 算子（`ai_infra_attention_pioneer_metadata`），比较「显式 AICPU 算子」与「框架级 tiling_sink」两条路线的工程差异。
- 想补齐 Host 侧全景：回看 u3-l3 的 `tiling_base` 责任链与 u8-l1/u8-l2 的 tiling UT 框架——它们分别决定 Host tiling「由谁做」与「怎么在没有 NPU 的情况下验证做对了」。
- 对 super kernel 与 AICPU RPC 的完整协议感兴趣的读者，可在已安装的 CANN 包 include 目录（如 `include/aicpu_common/context`）中查找相关上下文定义，本仓库只包含执行器一侧的实现。
