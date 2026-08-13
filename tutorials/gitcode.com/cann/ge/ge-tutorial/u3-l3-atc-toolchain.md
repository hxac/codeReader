# ATC 离线编译工具链

## 1. 本讲目标

在前两讲里，我们已经知道 GE 是一个「图编译器 + 执行器」，并且知道了 parser 模块如何用工厂模式把 ONNX/Caffe/TensorFlow 等前端模型翻译成 AscendIR。但 parser 只是「翻译官」，真正把用户在命令行敲下的一行命令变成可部署的 OM 文件的，是一个叫 **atc** 的离线编译工具。

本讲学完后，你应该能够：

- 说清 atc 在「离线场景」中扮演的角色，以及它和 parser、GE Compiler 的边界。
- 从源码层面描述 atc 的完整主流程：命令行参数如何被解析、如何被翻译成 GE 内部选项、如何调用解析器、如何驱动编译并产出 OM。
- 区分 atc 的多种工作模式（生成 OM、转 JSON、显示模型信息等），并知道离线编译产物 OM 的几种形态。

本讲是单元 3 的核心枢纽：它向上承接 [u3-l1 解析器框架](u3-l1-parser-framework.md)（调用 parser），向下衔接单元 4 的编译四阶段（atc 产出的图会进入 GraphManager 编译）。

## 2. 前置知识

阅读本讲前，请先确认你已经了解以下概念（若不熟悉，请回看对应讲义）：

- **GE 的两大入口**（见 u1-l1）：在线入口（TorchAir/TF Adapter，编译执行耦合）与离线入口（atc，编译执行分离）。本讲只讲离线入口 atc。
- **OM 文件**：GE 编译产出的离线模型二进制产物，可独立部署到昇腾设备执行。
- **AscendIR**：GE 的统一中间表示，是 parser 的唯一产出，也是编译器的输入。
- **parser 工厂模式**（见 u3-l1）：`ModelParserFactory`、`WeightsParserFactory` 按 `FrameworkType` 创建对应解析器，把外部模型转成 AscendIR。
- **命令行选项（flag）**：类似 gflags 的机制，用 `DEFINE_*` 宏声明一个全局变量（如 `FLAGS_model`），命令行上的 `--model=xxx` 会被解析后写入该变量。

一个关键直觉：**atc 本身不做"编译算法"，它是一个"调度器/胶水层"**。它的职责是：解析命令行 → 组装 GE 选项 → 调 parser 把模型翻译成 AscendIR → 把图和选项交给 GeGenerator（编译器入口）→ 产出 OM。理解了这点，本讲的源码就会变得清晰。

## 3. 本讲源码地图

本讲聚焦 `api/atc/` 目录，这是 atc 工具的全部实现所在。关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `api/atc/main.cc` | atc 可执行程序的真正入口，只有 `main()` 一行，转调 `ge::main_impl()`。 |
| `api/atc/main_impl.cc` | atc 的主逻辑文件（2300+ 行），含 `main_impl`、`RunAtcByMode`、`GenerateOmModel`、`GenerateModelBySingleGraph` 等核心函数。 |
| `api/atc/main_impl.h` | `main_impl` 的声明，供 `main.cc` 调用。 |
| `api/atc/omg.cc` | "omg"（Offline Model Generator）实现，含 `ParseGraph`——连接 atc 与 parser 的关键函数。 |
| `api/atc/atc_flags.cc` / `atc_flags.h` | 用 `DEFINE_*` 宏声明全部命令行选项全局变量。 |
| `api/atc/cmd_flag_info.cc` / `.h` | 命令行解析引擎，基于 `getopt_long` 实现 `ParseCommandLine`。 |
| `api/atc/atc_option_map.cc` | 把「GE 内部选项名」与「命令行可见名」做映射，用于错误提示与选项归一。 |
| `inc/framework/omg/omg.h` | `ParseGraph`、`ConvertOm` 等对外接口声明。 |
| `inc/framework/omg/omg_inner_types.h` | `RunMode` 枚举（mode 的取值定义）。 |
| `inc/graph_metadef/common/ge_common/ge_types.h` | `FrameworkType` 枚举（framework 的取值定义）。 |

> 构建侧补充（见 u1-l3）：`api/atc/CMakeLists.txt` 把这些源文件编成 `atc_static` 静态库，再链接成可执行程序 `atc_atc.bin`（即用户敲的 `atc` 命令），入口正是 `main.cc`。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**① atc 入口与选项**、**② omg 主流程**、**③ 离线编译产物**。

---

### 4.1 atc 入口与选项

#### 4.1.1 概念说明

atc 是一个命令行工具，用户这样使用它（生成 OM 的典型命令）：

```bash
atc --model=resnet50.onnx --framework=5 --output=resnet50 \
    --soc_version=AscendXXX --input_shape="input:1,3,224,224"
```

atc 需要解决三件事：

1. **入口**：从 C 语言标准的 `int main(int argc, char* argv[])` 开始，最终走到 atc 的逻辑。
2. **选项解析**：把 `--model=...`、`--framework=5` 这类字符串参数，解析并填充到程序里的全局变量。
3. **选项归一**：很多命令行选项最终要传给 GE 内部（编译器），需要一份「命令行名 ↔ GE 内部选项名」的对照表。

#### 4.1.2 核心流程

atc 入口与选项的执行顺序：

```
用户命令行 argv
      │
      ▼
main.cc::main()                    （C 标准入口）
      │  转调
      ▼
main_impl.cc::main_impl(argc, argv)（atc 真正主函数）
      │
      ├─► GFlagUtils::InitGFlag()   组装帮助信息 + 调用 ParseCommandLine
      │        └─► cmd_flag_info.cc::ParseCommandLine()
      │                 基于 getopt_long 逐个解析 --xxx，
      │                 填充 FLAGS_model / FLAGS_framework 等
      │
      ├─► init()                    设置日志级别、初始化错误管理
      │
      ├─► LoadRawOptionsForAtc()    加载 --raw_ge_options 等"原始 GE 选项"文件
      │
      ├─► CheckGlobalOptionsBeforeRun()  检查废弃/互斥选项
      │
      └─► RunAtcByMode()            按 --mode 分派到具体工作模式（见 4.2）
```

`--mode` 决定 atc 干什么活，取值定义在 `RunMode` 枚举里。

#### 4.1.3 源码精读

**（1）C 标准入口转调 `main_impl`**

[api/atc/main.cc:13-15](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main.cc#L13-L15)：`main.cc` 极其精简，只做一件事——把命令行参数原样转交给 `ge::main_impl`。这样设计是为了让 atc 的核心逻辑（`main_impl.cc`）既能编译成独立可执行程序（atc），也能被 Python 侧（pyatc）以库的形式复用。

```cpp
int32_t main(int32_t argc, char *argv[]) {
  return ge::main_impl(argc, argv);
}
```

**（2）atc 真正主函数 `main_impl`**

[api/atc/main_impl.cc:2345-2376](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2345-L2376)：这是 atc 的总控函数，五步走——初始化标志、初始化环境、加载原始选项、准备 Python 运行时、最终分派。

```cpp
int32_t main_impl(int32_t argc, char *argv[]) {
  Status ret = SUCCESS;
  std::cout << "ATC start working now, please wait for a moment." << std::endl;
  // 1. 解析命令行标志；若用户敲了 --help 直接返回
  const flgs::GfStatus flag = GFlagUtils::InitGFlag(argc, argv);
  if (flag == flgs::GF_HELP) { return 0; }
  if ((flag != flgs::GF_SUCCESS) || (init() != 0)) {
    return static_cast<int32_t>(CheckRet(-1));
  }
  // 2. 加载 --raw_ge_options 等原始选项
  std::map<std::string, std::string> raw_options;
  if (LoadRawOptionsForAtc(raw_options) != SUCCESS) { ... }
  // 3. 准备 Python 运行时（供 TBE/自定义算子编译用），并注册退出时清理钩子
  const auto python_runtime_ret = GePythonRuntimeManager::Instance().EnsureReady();
  GE_MAKE_GUARD(release_python_resources, []() { /* 卸载 pass 插件 / 自定义算子 / 关闭 python */ });
  // 4. 全局校验 + 按 mode 分派执行
  ret = (CheckGlobalOptionsBeforeRun() == SUCCESS && RunAtcByMode(raw_options) == SUCCESS) ? SUCCESS : FAILED;
  std::cout << "..." << std::endl;
  return static_cast<int32_t>(CheckRet(ret));
}
```

注意第 3 步的 `GE_MAKE_GUARD`：它注册了一个 RAII 守卫，保证无论 atc 成功还是失败退出，都会卸载已加载的融合 Pass 插件、自定义算子并关闭 Python 子进程，避免资源泄漏。

**（3）选项解析引擎：`InitGFlag` → `ParseCommandLine`**

[api/atc/main_impl.cc:521-727](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L521-L727)：`InitGFlag` 主要做两件事——先拼装一大段 `--help` 帮助文本（你在命令行敲 `atc --help` 看到的内容就是这里生成的），最后调用真正的解析函数：

```cpp
static flgs::GfStatus InitGFlag(int32_t argc, char *argv[]) {
  // ... 拼装 usage 帮助信息 ...
  return flgs::ParseCommandLine(argc, argv);
}
```

[api/atc/cmd_flag_info.cc:458-504](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/cmd_flag_info.cc#L458-L504)：解析引擎基于 POSIX 的 `getopt_long`（这里封装为 `mmGetOptLong`）逐个处理命令行 token，匹配到合法标志后调 `UpdateCmdFlagInfo` 把值写进对应的全局变量；遇到 `--help` 返回 `GF_HELP`，遇到未知参数或缺少参数返回 `GF_FAILED`。

```cpp
while ((index = mmGetOptLong(argc, buff, CMD_SHORT_OPTS, GetOptionsVec().data(), nullptr)) != -1) {
  if (index == FlagIndex::COLON) { /* 缺少参数 */ return GF_FAILED; }
  if (index == FlagIndex::QUESTION_MARK) { /* 未知参数 */ return GF_FAILED; }
  std::string value = (mmGetOptArg() == nullptr) ? "" : mmGetOptArg();
  ret = UpdateCmdFlagInfo(index, value);          // 写入 FLAGS_xxx
  if (index == FlagIndex::HELP) {                  // --help
    PrintUsageMessage();
    return GF_HELP;
  }
}
```

这些被写入的全局变量（如 `FLAGS_model`、`FLAGS_framework`、`FLAGS_mode`）在 [api/atc/atc_flags.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/atc_flags.cc) 里用 `DEFINE_*` 宏声明，例如：

```cpp
DEFINE_string(model, "", "The model file.");
DEFINE_int32(framework, -1, "Framework type(0:Caffe; 1:MindSpore; 3:Tensorflow; 5:Onnx).");
```

**（4）mode 与 framework 的取值**

atc 干什么活由 `--mode` 决定，[inc/framework/omg/omg_inner_types.h:35-43](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/omg_inner_types.h#L35-L43)：

```cpp
enum RunMode {
  GEN_OM_MODEL = 0,      // 生成离线 OM（默认）
  MODEL_TO_JSON = 1,     // 模型转 JSON
  ONLY_PRE_CHECK = 3,    // 仅做预检
  PBTXT_TO_JSON = 5,     // pbtxt 转 JSON
  DISPLAY_OM_INFO = 6,   // 显示模型信息
  GEN_OM2_MODEL = 7,     // 转成 OM2 格式
  GEN_EXE_OM = 10,
  MODEL_TO_EXE_OM = 20,
  GEN_EXE_OM_FOR_NANO = 30,
};
```

输入模型属于哪种前端由 `--framework` 决定，[inc/graph_metadef/common/ge_common/ge_types.h:31-37](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/common/ge_common/ge_types.h#L31-L37)：

```cpp
enum FrameworkType {
  CAFFE = 0,
  MINDSPORE = 1,
  TENSORFLOW = 3,
  ANDROID_NN = 4,
  ONNX = 5,
};
```

**（5）选项名映射表：把命令行名与 GE 内部名对齐**

atc 的很多命令行选项最终要转成 GE 编译器认识的内部选项名（如命令行 `--input_shape` 对应内部 `ge::ir_option::INPUT_SHAPE`）。[api/atc/atc_option_map.cc:21-97](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/atc_option_map.cc#L21-L97) 用一张静态对照表维护这层关系，[api/atc/atc_option_map.cc:101-107](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/atc_option_map.cc#L101-L107) 把它打包成 map 供错误提示与选项归一使用：

```cpp
const std::pair<std::string, std::string> kAtcGeOptionToCliName[] = {
    {ge::ir_option::INPUT_FORMAT, "--input_format"},
    {ge::ir_option::INPUT_SHAPE, "--input_shape"},
    {ge::SOC_VERSION, "--soc_version"},
    // ... 约 80 对映射 ...
};
std::map<std::string, std::string> BuildAtcGeOptionToCliNameMap() { /* 把数组转成 map */ }
```

这张表在后续 `SetOptionNameMap`（[api/atc/main_impl.cc:122-130](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L122-L130)）里被打包进选项 map 的 `OPTION_NAME_MAP` 字段，让编译器在报错时能把内部选项名翻译回用户熟悉的命令行名。

#### 4.1.4 代码实践

**实践目标**：从源码追踪「命令行字符串 `--framework=5`」是如何变成程序里可用的 `FLAGS_framework == 5` 的。

**操作步骤**：

1. 打开 [api/atc/atc_flags.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/atc_flags.cc)，找到 `DEFINE_int32(framework, ...)`，确认它声明了一个整型全局变量 `FLAGS_framework`，默认值 `-1`。
2. 打开 [api/atc/cmd_flag_info.cc:458-504](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/cmd_flag_info.cc#L458-L504)，确认 `ParseCommandLine` 在 `while` 循环里用 `mmGetOptLong` 逐个解析 token，命中后调 `UpdateCmdFlagInfo(index, value)`。
3. 顺着 `UpdateCmdFlagInfo` → `SetFlagValue`（同文件）找到它最终调用某个 `CmdFlagInfo::SetFlagValue`，把字符串 `"5"` 转成整数写进 `FLAGS_framework`。

**需要观察的现象**：`--help` 会让 `ParseCommandLine` 返回 `GF_HELP`，进而使 `main_impl` 直接 `return 0`，不进入任何编译流程；而一个拼错的参数（如 `--framewrok`）会命中 `QUESTION_MARK` 分支返回失败。

**预期结果**：你能画出「`--framework=5` → getopt 命中 → UpdateCmdFlagInfo → FLAGS_framework=5」这条链路。实际编译运行 atc 属于环境相关操作，若本地无昇腾环境则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：用户敲了 `atc --help`，程序为什么会直接退出而不报错？
**答案**：`ParseCommandLine` 检测到 `--help`（`FlagIndex::HELP`）后会调用 `PrintUsageMessage()` 打印帮助，并返回 `GF_HELP`；`main_impl` 第一阶段判断 `flag == flgs::GF_HELP` 即 `return 0`，不进入后续编译。

**练习 2**：`--framework` 的合法值有哪些？分别对应什么前端？
**答案**：见 `FrameworkType` 枚举——0=Caffe、1=MindSpore、3=TensorFlow、5=ONNX（4=Android_NN）。默认值 `-1` 表示未指定。

---

### 4.2 omg 主流程

#### 4.2.1 概念说明

「omg」是 Offline Model Generator 的缩写。如果说 `main_impl` 是 atc 的"前台总调度"，那么 omg 主流程就是它把"生成 OM"这件事真正落地的链路。这条链路要把命令行选项组装成 GE 编译器认识的 options，调用 parser 把模型文件翻译成 AscendIR（Graph），再交给 `GeGenerator` 驱动编译。

本模块要回答的核心问题是：**从 `RunAtcByMode` 到产出 OM，中间到底经历了哪些函数调用？**

#### 4.2.2 核心流程

生成 OM 的完整调用链（本讲最重要的图）：

```
RunAtcByMode(raw_options)                          main_impl.cc:2317
   │
   ├─ 若 --singleop 非空 ─► CheckAndRunSingleOp()   单算子分支
   │
   └─ IsGenerateOmMode() 为真 ─► GenerateOmModel(raw_options)   :2058
            │
            ├─ PrepareOmGeneration()                校验标志 + 加载自定义算子 .so
            ├─ PrepareAtcOptions(raw_options, opts) 把命令行选项组装成 GE options map
            ├─ AppendOutputFileSuffix()             给 output 自动加 .om/.om2 后缀
            │
            └─ GenerateModel(options, output)       :1696
                    │
                    ├─ GELib::Initialize(options)        初始化 GE 运行时（加载算子原型 .so 等）
                    ├─ ge_generator.Initialize(options)  初始化生成器
                    │
                    └─ GenerateModelBySingleGraph()      :1622
                            │
                            ├─ [非 MindSpore 分支] ParseGraph(graph, ...)   ★调用 parser★
                            │        └─ omg.cc:758
                            │             ├─ ModelParserFactory::CreateModelParser(type)
                            │             ├─ model_parser->Parse(model_file, graph)
                            │             ├─ WeightsParserFactory::CreateWeightsParser(type)
                            │             └─ weights_parser->Parse(weights_file, graph)
                            │
                            ├─ SetOutputNodeInfo(graph, output_type)  指定输出节点
                            ├─ SetAttrOptions(graph)                  keep_dtype 等图级属性
                            │
                            └─ GenerateOfflineModel(ge_generator, graph, output, inputs)  :1595
                                     └─ ge_generator.GenerateOfflineModel(graph, output, ...)
                                            └─ 进入 GE Compiler（单元 4 的四阶段编译）
```

带 ★ 的 `ParseGraph` 正是连接本讲与 [u3-l1 解析器框架](u3-l1-parser-framework.md) 的桥梁——它就是 u3-l1 里讲过的"统一解析入口"的真正实现。

#### 4.2.3 源码精读

**（1）按 mode 分派：`RunAtcByMode`**

[api/atc/main_impl.cc:2317-2342](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2317-L2342)：这是 atc 的"路由器"。先处理单算子分支，再做必要校验，然后根据 `FLAGS_mode` 把请求路由到四个工作模式之一。

```cpp
Status RunAtcByMode(const std::map<std::string, std::string> &raw_options) {
  OperatorFactoryImpl::BackupAndClearRegInfoOnce();   // 备份并清空算子注册表，防多次调用残留
  if (!FLAGS_singleop.empty()) { return CheckAndRunSingleOp(); }
  GE_CHK_BOOL_EXEC(GFlagUtils::CheckWeightAndFrameWork() && GFlagUtils::CheckSocVersionAndRunmode(), ...);
  GE_ASSERT_SUCCESS(UpdateCheckReportPath(), ...);
  if (IsGenerateOmMode()) { return GenerateOmModel(raw_options); }              // mode 0/3/7/10/30
  if (FLAGS_mode == RunMode::MODEL_TO_JSON) { ... ConvertModelToJson(); }       // mode 1
  if (FLAGS_mode == RunMode::PBTXT_TO_JSON)   { ... ConvertPbtxtToJson(); }     // mode 5
  if (FLAGS_mode == RunMode::DISPLAY_OM_INFO) { ... DisplayModelInfo(); }       // mode 6
  return ReportInvalidRunMode();                                                 // 其他 → 报错
}
```

[api/atc/main_impl.cc:2302-2308](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2302-L2308) 定义了哪些 mode 算"生成 OM 类"（即走 `GenerateOmModel`）：默认的 `GEN_OM_MODEL(0)`、仅预检 `ONLY_PRE_CHECK(3)`、OM2 `GEN_OM2_MODEL(7)`、`GEN_EXE_OM(10)`、nano `GEN_EXE_OM_FOR_NANO(30)`。

**（2）生成 OM 的门面：`GenerateOmModel`**

[api/atc/main_impl.cc:2058-2065](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2058-L2065)：三步准备 + 一步生成。

```cpp
Status GenerateOmModel(const std::map<std::string, std::string> &raw_options) {
  GE_ASSERT_SUCCESS(PrepareOmGeneration(), ...);               // 校验标志 + 加载自定义算子
  std::map<std::string, std::string> options;
  GE_ASSERT_SUCCESS(PrepareAtcOptions(raw_options, options), ...); // 组装 GE options
  AppendOutputFileSuffix();                                     // output 自动加后缀
  GE_CHK_BOOL_EXEC(GenerateModel(options, FLAGS_output) == SUCCESS, return FAILED, ...);
  return MaybeDisplayModelInfo();                               // 可选：打印模型信息
}
```

其中 [api/atc/main_impl.cc:2004-2022](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2004-L2022) 的 `PrepareAtcOptions` 会分门别类地把命令行选项塞进 `options` map（基础/目标/调优/调试/保存/环境/JIT 选项），再合并原始 GE 选项、附加优化选项、设置"离线构建模式"标志。

**（3）初始化 GE 并生成：`GenerateModel`**

[api/atc/main_impl.cc:1696-1720](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1696-L1720)：这里出现了一个关键对象 `GELib` 和 `GeGenerator`。`GELib::Initialize` 会初始化 GE 运行时（包括加载算子原型 `.so`，见 u2-l4），随后用 RAII 守卫保证退出时 `Finalize`。

```cpp
Status GenerateModel(std::map<std::string, std::string> &options, const std::string &output) {
  GeGenerator ge_generator;
  std::shared_ptr<GELib> instance_ptr = GELib::GetInstance();
  if (instance_ptr == nullptr || !instance_ptr->InitFlag()) {
    ret = GELib::Initialize(options);          // 首次调用：初始化 GE
  }
  ret = ge_generator.Initialize(options, domi::GetContext());
  const std::function<void()> callback = [&ge_generator]() {
    (void)ge_generator.Finalize();
    (void)GELib::GetInstance()->Finalize();
  };
  GE_MAKE_GUARD(release, callback);            // 退出自动清理
  return GenerateModelBySingleGraph(ge_generator, output, options);
}
```

**（4）解析 + 编译的衔接点：`GenerateModelBySingleGraph`**

[api/atc/main_impl.cc:1622-1694](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1622-L1694)：这是 atc 主流程里最关键的函数。它按 `framework` 分两条路：

```cpp
Status GenerateModelBySingleGraph(GeGenerator &ge_generator, const std::string &output,
                                  std::map<std::string, std::string> &options) {
  Graph graph;
  std::vector<GeTensor> inputs;
  if (FLAGS_framework == domi::MINDSPORE) {
    // MindSpore (.air)：模型本身就是 GE 的 Model 格式，直接 LoadFromFile 取出图
    Model load_model = Model("loadmodel", "version2");
    load_model.LoadFromFile(FLAGS_model);
    graph = GraphUtilsEx::CreateGraphFromComputeGraph(load_model.GetGraph());
    CreateInputsForInference(graph, inputs);
  } else {
    // Caffe / TensorFlow / ONNX：调用 parser 把外部模型翻译成 AscendIR
    std::map<std::string, std::string> atc_params;
    SetAtcParams(atc_params, output);                                    // :1608 收集 input_shape 等
    ret = ParseGraph(graph, atc_params, FLAGS_model.c_str(), FLAGS_weight.c_str(),
                     static_cast<domi::FrameworkType>(FLAGS_framework),
                     FLAGS_op_name_map.c_str(), FLAGS_target.c_str(),
                     static_cast<RunMode>(FLAGS_mode), is_dynamic_input);  // ★进入 omg.cc★
    if (FLAGS_mode == static_cast<int32_t>(RunMode::ONLY_PRE_CHECK)) { return SUCCESS; } // 仅预检到此为止
    SetOutputNodeInfo(graph, FLAGS_output_type);                         // 指定输出节点
  }
  SetAttrOptions(graph);                                                 // keep_dtype / 压缩等属性
  CallAmctInterface(graph, options);                                     // 可选：量化工具
  return GenerateOfflineModel(ge_generator, graph, output, inputs);      // 交给编译器
}
```

注意三点：① MindSpore 走的是"直接加载"而非 parser，因为 `.air` 已是 GE 原生格式；② `ONLY_PRE_CHECK`（mode 3）模式在 `ParseGraph` 内部就已生成预检报告，这里直接返回，不再编译；③ 真正进入编译器的是 `GenerateOfflineModel`。

**（5）连接 parser 的枢纽：`ParseGraph`（omg.cc）**

[api/atc/omg.cc:758-887](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L758-L887)：这正是 u3-l1 讲过的"统一解析入口"。它的内部用到了 u3-l1 的两个工厂：

```cpp
domi::Status ParseGraph(ge::Graph &graph, ..., domi::FrameworkType type, ...) {
  // 1. 建空图 + 初始化上下文（input_shape / input_format / out_nodes）
  ComputeGraphPtr compute_graph = MakeShared<ComputeGraph>(graph_name);
  graph = GraphUtilsEx::CreateGraphFromComputeGraph(compute_graph);
  InitDomiOmgContext(input_shape, input_format, "", is_dynamic_input);

  // 2. ★用 ModelParserFactory 按 framework 类型创建模型解析器并解析★
  auto model_parser = domi::ModelParserFactory::Instance()->CreateModelParser(type);
  UpdateParserCtxWithOmgCtx();
  domi::Status ret = model_parser->Parse(model_file, graph);   // ONNX/Caffe/TF → AscendIR
  UpdateOmgCtxWithParserCtx();

  compute_graph->Dump();   // 打印解析后的图结构（日志）

  // 3. ★用 WeightsParserFactory 解析独立权重文件（Caffe 场景）★
  auto weights_parser = domi::WeightsParserFactory::Instance()->CreateWeightsParser(type);
  ret = weights_parser->Parse(weights_file, graph);

  // 4. 更新动态 shape range 等
  UpdateDynamicInputShapeRange(compute_graph, input_shape_range);
  return SUCCESS;
}
```

`ModelParserFactory::CreateModelParser(type)` 正是 u3-l1 介绍的工厂模式——传 `ONNX(5)` 返回 OnnxModelParser，传 `CAFFE(0)` 返回 Caffe 解析器。`ParseGraph` 把"建图、解析结构、解析权重、预检报告"这些步骤串起来，最终产出一个填好的 `ge::Graph`（AscendIR）。解析器 `.so` 的查找则在 [api/atc/omg.cc:243-285](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L243-L285) 的 `FindParserSo` 中递归扫描目录完成。

#### 4.2.4 代码实践

**实践目标**：从源码梳理 atc「从命令行参数 → 调用解析器 → 产出 OM」的关键调用顺序，并把这条链路画出来。

**操作步骤**：

1. 从 [main_impl.cc:2345](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2345) 的 `main_impl` 出发，确认它最终调用 `RunAtcByMode`。
2. 进入 [main_impl.cc:2317](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2317) `RunAtcByMode`，找到 `GenerateOmModel` → `GenerateModel` → `GenerateModelBySingleGraph`。
3. 在 [main_impl.cc:1657](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1657) 找到对 `ParseGraph` 的调用，再跳到 [omg.cc:758](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L758) 的定义，确认它调了 `ModelParserFactory::CreateModelParser`（u3-l1 的工厂）。
4. 回到 [main_impl.cc:1687](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1687) 的 `GenerateOfflineModel`，确认解析后的图在此交给 `GeGenerator`。

**需要观察的现象**：在 `GenerateModelBySingleGraph` 中，`FLAGS_framework == domi::MINDSPORE` 走"直接加载"分支，其余 framework 都走 `ParseGraph` 分支；`ONLY_PRE_CHECK` 模式在 `ParseGraph` 返回后即 `return SUCCESS`，不会走到 `GenerateOfflineModel`。

**预期结果**：你能复现 4.2.2 的调用链图，并能指出 `ParseGraph` 是 atc 与 parser 工厂体系的唯一衔接点。实际运行 atc 需昇腾环境，若本地无设备则相关运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MindSpore（framework=1）不走 `ParseGraph`，而 ONNX/TensorFlow/Caffe 走？
**答案**：MindSpore 的 `.air` 模型本身就是 GE 的原生 `Model` 格式（内含 ComputeGraph），所以 `GenerateModelBySingleGraph` 直接用 `load_model.LoadFromFile` 取出图，无需"翻译"；而 ONNX/Caffe/TensorFlow 是外部格式，必须经 `ParseGraph` 用对应的 parser 翻译成 AscendIR。

**练习 2**：`GELib::Initialize` 在 `GenerateModel` 里被调用，如果 atc 一次运行只编译一个模型，为什么还要判断 `instance_ptr->InitFlag()`？
**答案**：`GELib` 是单例（`GetInstance()`）。判断 `InitFlag()` 是为了在 GE 已被初始化过（例如被宿主进程复用 atc 作为库的场景，或 pyatc）时，不重复初始化，避免资源重复加载与状态污染。

---

### 4.3 离线编译产物

#### 4.3.1 概念说明

前面两模块讲的是"怎么编译"，本模块讲"编出来是什么"。atc 的最终产物不止一种：最常见的是 `.om`，此外还有 `.om2`（v2 执行器格式）、`.exeom`（nano 芯片的预加载格式），以及非编译类产物（JSON、模型信息文本）。理解产物形态有助于明白 atc 不同 `--mode` 的本质差异。

#### 4.3.2 核心流程

atc 的产物由 `FLAGS_mode` 决定，可归为三类：

| mode | 模式 | 走的函数 | 产物 |
| --- | --- | --- | --- |
| 0 (默认) | 生成离线模型 | `GenerateOmModel` | `<output>.om` |
| 7 | 生成 OM2 | `GenerateOmModel` | `<output>.om2` |
| 30 | nano exe-om | `GenerateOmModel` | `<output>.exeom` (+ `.dbg`) |
| 3 | 仅预检 | `ParseGraph` 内部 | `check_result.json`（不产 OM） |
| 1 | 模型转 JSON | `ConvertModelToJson` | `<output>.json` |
| 5 | pbtxt 转 JSON | `ConvertPbtxtToJson` | `<output>.json` |
| 6 | 显示模型信息 | `DisplayModelInfo` → `ConvertOm` | 打印到终端 / 可选 JSON |

#### 4.3.3 源码精读

**（1）OM 的产出入口：`GenerateOfflineModel`**

[api/atc/main_impl.cc:1595-1606](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1595-L1606)：根据 mode 选择 OM 的格式枚举，最终把图交给 `GeGenerator`。`GeGenerator::GenerateOfflineModel` 之后便进入单元 4 的编译四阶段，本讲不深入。

```cpp
static Status GenerateOfflineModel(GeGenerator &ge_generator, Graph graph, std::string output,
                                   std::vector<GeTensor> inputs) {
  std::map<int32_t, OfflineModelFormat> flags_mode_map = {
      {GEN_EXE_OM_FOR_NANO, OfflineModelFormat::OM_FORMAT_NANO},
      {GEN_OM2_MODEL, OfflineModelFormat::OM_FORMAT_OM2},
  };
  if (flags_mode_map.find(FLAGS_mode) != flags_mode_map.end()) {
    return ge_generator.GenerateOfflineModel(graph, output, inputs, flags_mode_map[FLAGS_mode]);
  }
  return ge_generator.GenerateOfflineModel(graph, output, inputs, OfflineModelFormat::OM_FORMAT_DEFAULT);
}
```

**（2）输出文件后缀的自动追加：`AppendOutputFileSuffix`**

[api/atc/main_impl.cc:86-89](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L86-L89) 定义了 mode 到后缀的映射，[api/atc/main_impl.cc:2024-2031](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2024-L2031) 在生成前给 `FLAGS_output` 追加后缀——这就是为什么用户敲 `--output=resnet50` 最终得到的是 `resnet50.om`。

```cpp
const std::string kFilePreffix(".om");
const std::string kOm2FilePreffix(".om2");
const std::string kPreloadFilePreffix(".exeom");
const std::map<ge::RunMode, std::string> kFilePrefixMap = {
    {ge::GEN_EXE_OM_FOR_NANO, kPreloadFilePreffix},
    {ge::GEN_OM2_MODEL, kOm2FilePreffix},
};

void AppendOutputFileSuffix() {
  const auto it = kFilePrefixMap.find(static_cast<RunMode>(FLAGS_mode));
  if (it == kFilePrefixMap.end()) { FLAGS_output += kFilePreffix; }   // 默认 .om
  else { FLAGS_output += it->second; }                                // nano → .exeom, om2 → .om2
}
```

**（3）非编译类产物：转 JSON 与显示信息**

当用户只想"查看"而非"编译"模型时，atc 不需要走编译器。以 [api/atc/main_impl.cc:1506-1527](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1506-L1527) 的 `ConvertModelToJson` 为例：若 `framework == -1`，输入是已编译的 OM 文件，直接调 `ConvertOm` 把二进制 OM 反序列化成可读 JSON；否则把 Caffe/TF 原始模型转 JSON。

```cpp
static Status ConvertModelToJson(int32_t fwk_type, const std::string &model_file, const std::string &json_file) {
  if (fwk_type == -1) {                       // 输入是 OM
    return ConvertOm(model_file.c_str(), json_file.c_str(), true);
  }
  // 输入是 Caffe/TF：依赖对应 parser so
  if (FLAGS_dump_mode == "0") {
    LoadCustomOpLib(false);
    return ConvertFwkModelToJson(static_cast<domi::FrameworkType>(fwk_type), model_file.c_str(), json_file.c_str());
  } else if (FLAGS_dump_mode == "1") {
    LoadCustomOpLib(true);
    return GenerateInfershapeJson();
  }
  return ret;
}
```

`ConvertOm` 在 [inc/framework/omg/omg.h:61](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/omg.h#L61) 声明，负责 OM 二进制 ↔ JSON 的互转，是排查 OM 内容时常用的工具。

> 小结：OM 是**自包含的可部署二进制**（内含编译后的算子、权重、流/内存规划）。`.om2` 对应 v2 执行器（见 u6-l1 运行时双版本），`.exeom` 是 nano 芯片的预加载形态。mode 1/5/6 则是"只读不编"的辅助工具。

#### 4.3.4 代码实践

**实践目标**：通过源码弄清 `--output=resnet50` 在不同 mode 下会产出什么文件。

**操作步骤**：

1. 打开 [main_impl.cc:86-89](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L86-L89) 的 `kFilePrefixMap`，确认只有 `GEN_EXE_OM_FOR_NANO` 和 `GEN_OM2_MODEL` 两个 mode 在表里。
2. 读 [AppendOutputFileSuffix](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2024-L2031)：表中找不到时（如默认 mode 0）追加 `.om`，否则追加表中的值。
3. 对照 [GenerateOfflineModel](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L1595-L1606)：mode 0 → `OM_FORMAT_DEFAULT`，mode 7 → `OM_FORMAT_OM2`，mode 30 → `OM_FORMAT_NANO`。

**需要观察的现象**：mode 决定两个独立的事——①文件后缀（`.om`/`.om2`/`.exeom`）；②传给编译器的 `OfflineModelFormat` 枚举（影响 OM 内部序列化格式）。

**预期结果**：填出下表。实际编译产物需在昇腾环境运行 atc 后核对，若无设备则「待本地验证」。

| 命令 | mode | 输出文件 | OM 格式枚举 |
| --- | --- | --- | --- |
| `atc --output=m ...`（默认） | 0 | `m.om` | OM_FORMAT_DEFAULT |
| `atc --mode=7 --output=m ...` | 7 | `m.om2` | OM_FORMAT_OM2 |
| `atc --mode=30 --output=m ...` | 30 | `m.exeom` | OM_FORMAT_NANO |

#### 4.3.5 小练习与答案

**练习 1**：用户希望把一个已编译好的 `.om` 文件转成可读 JSON 来排查问题，应该用哪条 atc 命令？为什么不需要 `--soc_version`？
**答案**：用 `atc --mode=1 --framework=-1 --om=model.om --json=model.json`。`framework=-1` 表示输入是 OM（已是 GE 原生产物），走 `ConvertModelToJson` → `ConvertOm` 的反序列化分支，只做格式转换、不重新编译，因此不需要指定 `--soc_version`（它只在编译时用来选算子实现）。

**练习 2**：`ONLY_PRE_CHECK`（mode 3）会产出 OM 吗？
**答案**：不会。`RunAtcByMode` 虽然把 mode 3 也算作"生成 OM 类"（`IsGenerateOmMode()` 为真）并进入 `GenerateOmModel`，但 `ParseGraph` 内部会在预检完成时生成 `check_result.json` 报告；随后 `GenerateModelBySingleGraph` 检测到 `FLAGS_mode == ONLY_PRE_CHECK` 直接 `return SUCCESS`，不会调 `GenerateOfflineModel`，故不产 OM。

---

## 5. 综合实践

**任务**：充当一次"atc 源码侦探"，把整条离线编译链路串起来，并解释一个真实命令的每一步。

给定命令（生成 ONNX 模型的 OM）：

```bash
atc --model=resnet50.onnx --framework=5 --output=resnet50 \
    --soc_version=AscendXXX --input_shape="input:1,3,224,224"
```

请完成：

1. **追踪 argv 的旅程**：从 [main.cc:13](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main.cc#L13) 到 `ParseCommandLine`，写出 `--framework=5` 是如何变成 `FLAGS_framework == 5` 的（参考 4.1.3）。
2. **解释 mode 的默认值**：本命令没有 `--mode`，从 [atc_flags.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/atc_flags.cc) 的 `DEFINE_int32(mode, ...)` 确认默认值，并据此判断 `RunAtcByMode` 会走哪条分支（`GenerateOmModel`）。
3. **定位 parser 的调用**：说明 `framework=5`（ONNX）会如何影响 [omg.cc:819](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L819) `CreateModelParser(type)` 的返回值（OnnxModelParser），并指出它承接了 u3-l1 的哪个工厂。
4. **预测产物**：写出最终生成的文件名（`resnet50.om`）并说明依据（[AppendOutputFileSuffix](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2024-L2031)）。
5. **画出完整链路图**：综合 4.1.2 与 4.2.2，画一张从 `main` 到 `GeGenerator::GenerateOfflineModel` 的完整调用图，标出 parser 的接入点与 OM 的产出点。

**参考要点**：第 3 步是本综合实践的核心——它要求你把本讲的 `ParseGraph` 与 u3-l1 的 `ModelParserFactory` 串联起来，这正是 atc 作为"调度层"调用 parser 的本质。第 5 步的图应当与 4.2.2 一致，并能标注出「 AscendIR 在 `ParseGraph` 返回后形成」「编译器入口在 `GenerateOfflineModel`」两个关键节点。

> 运行验证：若本地有昇腾环境与 CANN 工具链，可实际执行上述命令，观察终端先打印 `ATC start working now`（[main_impl.cc:2347](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc#L2347)），最后在当前目录得到 `resnet50.om`，并用 `atc --mode=1 --framework=-1 --om=resnet50.om --json=resnet50.json` 反查其内容。若无环境，以上为源码阅读型实践，运行结果「待本地验证」。

## 6. 本讲小结

- **atc 是离线编译入口，本质是调度层**：它本身不做编译算法，而是负责解析命令行、组装 GE 选项、调 parser、再把图交给编译器（`GeGenerator`）。
- **入口极简**：`main.cc::main` 只有一行，转调 `main_impl.cc::main_impl`，后者按"初始化标志 → 初始化环境 → 加载原始选项 → 按 mode 分派"五步推进。
- **选项机制**：命令行经 `InitGFlag → ParseCommandLine`（基于 `getopt_long`）解析进 `FLAGS_*` 全局变量；`atc_option_map.cc` 维护命令行名与 GE 内部选项名的对照表。
- **mode 是总路由**：`RunAtcByMode` 按 `RunMode` 枚举把请求分派到生成 OM、转 JSON、显示信息等模式；生成 OM 类（mode 0/3/7/10/30）走 `GenerateOmModel`。
- **`ParseGraph` 是与 parser 的唯一衔接点**：[omg.cc:758](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L758) 用 `ModelParserFactory`/`WeightsParserFactory`（u3-l1 的工厂）把外部模型翻译成 AscendIR，承接了单元 3 前两讲。
- **产物由 mode 决定**：默认产出 `<output>.om`，mode 7 产出 `.om2`，mode 30 产出 `.exeom`；mode 1/5/6 是只读不编的辅助工具，mode 3 只产预检报告。

## 7. 下一步学习建议

本讲把 atc 这条"离线编译链路"讲到了 `GeGenerator::GenerateOfflineModel`——这正是进入 **GE Compiler** 的大门。建议接下来：

1. **进入编译器总览**：学习 [u4-l1 编译总览：GraphManager 与 CompilerStages](u4-l1-compiler-overview.md)，看 atc 交出的图如何在编译器里走完"预处理 → 图优化 → 引擎分区 → 构建"四阶段。
2. **深入 atc 调用的 parser**：若对 `ParseGraph` 内部如何把 ONNX 翻译成 AscendIR 感兴趣，回看 [u3-l2 ONNX 模型解析实战](u3-l2-onnx-parser.md)。
3. **理解 OM 的另一端**：编译产出的 OM 如何被执行器加载，见 [u6-l2 v1 静态执行器：模型加载与 DavinciModel](u6-l2-v1-davinci-model.md)。
4. **进阶阅读源码**：通读 [api/atc/main_impl.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/main_impl.cc) 的选项组装函数（`SetAtcBasicOptions` 等）与 [api/atc/omg.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc) 的 `SetOutputNodeInfo`、`InitDomiOmgContext`，理解选项如何精细控制编译行为。
