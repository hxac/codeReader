# OM2 模型格式与运行时执行

## 1. 本讲目标

在前面的单元里，你已经见过 GE 运行时的两套执行架构：v1（静态 shape，把 OM 反序列化成 `DavinciModel` 后用 `rtModelExecute` 硬件 Sink 执行）与 v2（动态 shape，把 `ComputeGraph` 经 Lowering 转成 `ExecuteGraph`，由 Host 顺序/拓扑执行）。本讲要介绍的是**第三条路径——OM2**。

OM2 是一种「把编译产物再编译一遍」的新一代离线模型格式：编译器不再只产出一份二进制任务清单（OM），而是**为整张模型生成 C++ 源码、编译成 `.so`、再打包成一个 ZIP 归档**。运行时不再「解释」任务清单，而是 `dlopen` 这份 `.so`、通过函数指针直接调用编译出来的原生代码。

学完本讲，你应当能够：

1. 说清 OM2 归档里到底装了哪些产物（so/json/权重/变量），以及它是如何用 ZIP 结构把它们组织起来的。
2. 描述 `Om2ModelExecutor` 如何通过 **memfd_create → dlopen → dlsym → 函数指针** 这条链路把一份 `.so` 加载成可执行模型，并能跟踪 Create/Load/Run 的调用顺序。
3. 理解 `Om2RTVarManager` 如何为 Variable 分配设备地址、如何把权重数据搬运到设备、以及三种数据加载路径（init_data / 转换 / 拷贝）的分工。
4. 能够把 OM2 与 v1/v2 放在同一张表里对比，说清「编译出来的原生代码执行」与「运行时解释执行」的本质差异。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**直觉一：从「解释」到「编译」。** v1 的 OM 文件本质是一份序列化的任务清单（protobuf `TaskDef`），`DavinciModel` 在加载时逐条「解释」它、把任务下沉到硬件。OM2 换了个思路：既然模型结构在编译期已经完全确定，为什么不直接**把整个模型的加载与执行逻辑生成成 C++ 代码**？这样运行时拿到的是已经编译好的机器码，不再需要通用解释器。可以类比「脚本 vs 编译出的可执行文件」：OM 是脚本，OM2 是可执行文件。

**直觉二：ZIP 是容器，so 是程序，json/权重是数据。** 一个 `.om2` 文件其实就是一个普通 ZIP 包（前 4 字节就是 ZIP 的魔数 `PK\x03\x04`）。里面分门别类放着：编译产物 so、模型元数据 json、常量权重、变量资源、算子 kernel 二进制、调试信息。运行时按**路径前缀**分派（`/runtime/`、`data/constants/`、`data/variables/`、`/debug/` ……），各取所需。

**直觉三：函数指针是 host 与 generated so 的「契约」。** 编译器生成的 so 必然要导出一组**固定名字**的 C 函数供运行时调用——这就是 `Om2ModelCreate / Om2ModelLoad / Om2ModelRun / Om2ModelRunAsync / Om2ModelDestroy` 五个符号。运行时用 `dlsym` 按名字取到它们、转成强类型函数指针，之后对模型的全部操作都退化为「调用这五个指针」。这组符号名就是 host 与 generated so 之间唯一的契约。

> 名词速查：**H2D / D2H** = Host↔Device 内存拷贝（`ACL_MEMCPY_HOST_TO_DEVICE` / `ACL_MEMCPY_DEVICE_TO_HOST`）；**Variable** = 训练中保存下来、推理时加载的参数（权重）节点；**memfd_create** = Linux 系统调用，在内存中创建一个「匿名文件」，可通过 `/proc/<pid>/fd/<fd>` 路径访问，无需落盘即可被 `dlopen`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [runtime/om2/om2_model_executor.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc) | **核心**。OM2 模型的反序列化、so 加载、函数指针解析、Create/Load/Run 全流程都在这里。 |
| [inc/framework/runtime/om2_model_executor.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/inc/framework/runtime/om2_model_executor.h) | `Om2ModelExecutor` 对外接口与 `Om2ModelLoadArg` 加载参数定义。 |
| [runtime/om2/om2_model_manager.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_manager.h) / [.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_manager.cc) | 进程级单例 `Om2ModelManager`，按 model_id 管理 executor 生命周期（对应 v1 的 ModelManager）。 |
| [runtime/om2/zip_archive_reader.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/zip_archive_reader.h) | `RAIIZipArchive`：基于内存的只读 ZIP 读取器，提供 `ListFiles / ExtractToMem / HasEntry`。 |
| [base/common/om2/om2_model_data.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/om2_model_data.h) | `Om2ModelData`：反序列化后、内存中的「模型全量数据」聚合结构。 |
| [base/common/om2/codegen/om2_codegen.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen.h) / [.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen.cc) | **编译侧**入口。`Om2Codegen::Om2CodegenAndCompile` 把 GeModel 生成源码并编译成 so。 |
| [base/common/om2/codegen/file_code_generator/interface_file_code_generator.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/file_code_generator/interface_file_code_generator.cc) | 生成 so 对外导出的 5 个 C 函数声明（host↔so 契约的源头）。 |
| [runtime/om2/om2_rt_var_manager.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.h) / [.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc) | `Om2RTVarManager`：Variable 的设备地址分配、权重搬运与格式转换。 |
| [base/common/om2/rt_var_resource.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/rt_var_resource.h) | `RTVarEntry / RTVarResource`：单个 Variable 的描述与集合。 |

> 两条命名空间线索：编译/数据结构多在 `namespace ge`（`Om2Codegen`、`Om2ModelData`、`Om2VarMeta`），运行时执行器与变量管理在 `namespace gert`（`Om2ModelExecutor`、`Om2RTVarManager`）。

## 4. 核心概念与源码讲解

### 4.1 OM2 模型格式与 ZIP/so 产物

#### 4.1.1 概念说明

OM2 想解决的核心问题是：**v1 的 OM 是「数据」，而 OM2 让模型本身变成「程序」**。

在 v1 里，`GeModel` 经 protobuf 序列化成 OM，运行时 `DavinciModel` 把它读回来逐条下发。这条路径里有一个常驻的「解释器」。OM2 的做法是：编译结束时，用 **codegen** 把整张图「翻译」成一份 C++ 源码——里面是一个 `Om2Model` 类，它的构造函数负责注册 kernel、它的 `Run` 方法负责按编译期确定好的顺序逐个 `aclrtKernelLaunch`。把这份源码编译成动态库 `lib<模型名>_om2.so`，再把 so、权重、元数据一起打包进 ZIP，就得到了一个 `.om2` 文件。

这样一来，模型加载退化成 `dlopen(so)`，模型执行退化成调用 so 里的 `Om2ModelRun`——没有任何通用解释开销，且编译器对图的全部知识都被「固化」进了原生代码。

#### 4.1.2 核心流程

一个 OM2 归档的生命周期分为「编译侧生成」和「运行侧识别」两端：

**编译侧（生成 OM2）**：

1. 编译器四阶段产出 `GeModel`（含已编译的 kernel、TaskDef 序列）。
2. `Om2Codegen::Om2CodegenAndCompile(ge_model)` 把 `GeModel` 喂给 codegen：
   - `Om2CodegenModelBuilder` + `ProgramGenerator` 把图翻译成 AST，`Om2CodePrinter` 打印成若干 C++ 源文件。
   - `Om2Utils::CompileGeneratedCppToSo` 调用编译器把源码编成 `lib<模型名>_om2.so`。
3. 把 so、源码、kernel 二进制、权重、元数据、manifest 按目录约定打包成 ZIP。

**运行侧（识别 OM2）**：

1. `IsOm2Model` 检查文件/内存前 4 字节是否等于 ZIP 魔数 `PK\x03\x04`。
2. 命中则按 OM2 流程走（否则按 v1 OM 流程）。

ZIP 内部的目录约定（这是 host 与打包工具之间的隐式契约）：

| ZIP 内路径模式 | 内容 | 反序列化目标 |
| --- | --- | --- |
| `*/runtime/*.so` | 编译产物动态库 | `program_body.so_artifact` |
| `*/model_meta.json` | 输入输出/动态档位/work_size 等元信息 | `model_meta` |
| `data/constants/*_constants_config.json` | 常量清单（index/type/offset/size） | `constants_data` |
| `data/constants/constant_*` | 拼接好的内部权重 buffer | `constants_data.weight_data` |
| `data/variables/var_resource.json` + `var_weight_data` | Variable 资源与初始权重 | `rt_var_resource` |
| `data/variables/*_variables_config.json` | Variable 元信息列表 | `var_metas` |
| `data/kernels_*.o` | 已编译算子二进制（AICore/AICPU） | `kernel_binaries` |
| `*/debug/op_attr.json`、`*/debug/ge_visual_*.json` | 调试/可视化信息 | `debug_info` |
| `manifest.json` | 归档清单 | `manifest` |

#### 4.1.3 源码精读

**① OM2 魔数识别**——OM2 复用标准 ZIP 头作为魔数，前 4 字节匹配即判定为 OM2：

[runtime/om2/om2_model_executor.cc:46-47](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L46-L47) 定义 `FILE_MAGIC_HEADER_SIZE` 与 `OM2_MAGIC = {0x50, 0x4B, 0x03, 0x04}`（即 ASCII `PK\x03\x04`）。

[runtime/om2/om2_model_executor.cc:1660-1681](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1660-L1681) 的 `IsOm2Model(data, size)` 做空指针/长度校验后，用 `std::memcmp(data, OM2_MAGIC, 4)` 判定，写回 `is_support`。

**② ZIP 条目按路径分派**——这是理解整个格式的钥匙。`HandleArchiveEntry` 对每个 entry 做**前缀 + 后缀**匹配：

[runtime/om2/om2_model_executor.cc:488-529](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L488-L529)：例如 `entry.find("/runtime/")` 且以 `.so` 结尾 → 走 `DeserializeCodegenEntry`；含 `/debug/` → 算子属性或可视化 json；以 `model_meta.json` 结尾 → 元数据；`data/constants/`、`data/variables/`、`data/kernels_*.o` 各自归位。顶层 `DeserializeOm2ModelDataFromArchive`（[531-546](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L531-L546)）遍历所有 entry 调它，并强校验「必须有 model_meta.json、必须有 .so」。

**③ 内存中的全量模型数据**——反序列化的终点是 `Om2ModelData`：

[base/common/om2/om2_model_data.h:82-92](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/om2_model_data.h#L82-L92) 定义 `Om2ModelData`，它把 ZIP 里所有产物聚合到一个结构体：`program_body`（含 `so_artifact`）、`model_meta`、`constants_data`、`kernel_binaries`、`debug_info`、`manifest`、`rt_var_resource`、`var_metas`、`graph_id`。其中 `Om2ProgramBody`（[34-37](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/om2_model_data.h#L34-L37)）同时保留 `source_artifacts`（源码）与 `so_artifact`（编译产物）。

**④ so 是怎么被「生成」出来的**——回到编译侧：

[base/common/om2/codegen/om2_codegen.cc:58-91](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen.cc#L58-L91)：`Om2CodegenAndCompile` 先 `CreateTaskCodeBuilders` + `builder.Build` 把 `GeModel` 翻译成 `Om2CodegenModel`，再用 `ProgramGenerator` + `Om2CodePrinter` 打印源码；随后在第 [78-81](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen.cc#L78-L81) 行把产物命名为 `lib<模型名>_om2.so` 并 `CompileGeneratedCppToSo`。若编译失败，会把生成的源码 dump 到 `/tmp/.tmp_om2_workspace` 便于排查（[33-55](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen.cc#L33-L55)）。

**⑤ so 对外导出的契约符号**——codegen 生成的源码会声明并实现 5 个固定名字的 C 函数：

[base/common/om2/codegen/file_code_generator/interface_file_code_generator.cc:202-227](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/file_code_generator/interface_file_code_generator.cc#L202-L227) 的 `BuildExternalApiDecls` 依次声明 `Om2ModelCreate / Om2ModelLoad / Om2ModelRunAsync / Om2ModelRun / Om2ModelDestroy`。注意 `Om2ModelCreate` 的参数表（bin 文件/数据/大小、constants、var_addrs、work_ptr、session_id、model_id、instance_handle）——这正是运行时要凑齐交给它的「弹药」。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用一张表把「ZIP 条目 → 内存字段」的映射亲手对一遍，确认你真的看懂了格式。

**操作步骤**：

1. 打开 ST 测试 [tests/ge/st/testcase/test_om2.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc)，阅读第 [2185-2191](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc#L2185-L2191) 行 `ExpectOm2ArchiveFiles` 期望的归档文件列表：
   - `fake_test/data/model_0/runtime/libg1_om2.so`
   - `fake_test/data/constants/model_0_constants_config.json`
   - `fake_test/data/model_0/model_meta.json`
   - `fake_test/data/model_0/debug/op_attr.json`
   - `fake_test/manifest.json`
2. 对每个文件路径，回到 `HandleArchiveEntry`（[488-529](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L488-L529)）判断它会被哪一条 `if` 命中、进入哪个 `Deserialize*` 函数、最终写到 `Om2ModelData` 的哪个字段。

**需要观察的现象 / 预期结果**：你应当得出类似下表的对应关系（「待本地验证」的部分：实际归档中是否还包含 `data/variables/`、`data/kernels_*.o` 取决于具体模型是否含 Variable 与离线 kernel，可对照源码确认这些分支存在即可）。

| ZIP 条目 | 命中分支 | 写入字段 |
| --- | --- | --- |
| `.../runtime/libg1_om2.so` | `/runtime/` + `.so` | `program_body.so_artifact` |
| `..._constants_config.json` | `data/constants/` + `_constants_config.json` | `constants_data.consts / internal_weight_size` |
| `model_meta.json` | `model_meta.json` 结尾 | `model_meta` |
| `debug/op_attr.json` | `/debug/` + `op_attr.json` | `debug_info.op_attr_map` |
| `manifest.json` | （由其它打包逻辑读取） | `manifest` |

#### 4.1.5 小练习与答案

**练习 1**：为什么 OM2 直接复用 ZIP 魔数 `PK\x03\x04` 作为格式标识，而不是自定义一个独立魔数？

**参考答案**：因为 `.om2` 本身就是一个标准 ZIP 容器，复用 ZIP 魔数让 `IsOm2Model` 只需 `memcmp` 前 4 字节即可零成本识别，且天然复用了 ZIP 的目录/压缩/随机访问能力；真正的「OM2 语义」由内部目录约定（`/runtime/*.so`、`model_meta.json` 等）承载，无需在文件头里重复编码。

**练习 2**：如果一份 `.om2` 归档里**缺少 `.so`**，加载会在哪一步、以什么错误失败？

**参考答案**：在 `DeserializeOm2ModelDataFromArchive`（[531-546](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L531-L546)）结尾的强校验 `!model_data.program_body.so_artifact.file_name.empty()` 处失败，提示 `Compiled .so not found in ZIP archive.`，整个 `Load` 提前返回错误，不会进入后续 dlopen。

---

### 4.2 om2_model_executor 加载执行

#### 4.2.1 概念说明

`Om2ModelExecutor` 是 OM2 在运行时的「门面 + 引擎」。它对外只暴露 `Load / Run / RunAsync` 以及一组查询接口（描述、动态档位、AIPP 等），对内用一个 Pimpl 的 `Impl` 类把全部细节藏起来。

它的核心使命是：**把一份内存里的 `.so` 字节流，变成一个可被反复 Run 的模型实例**。这件事的难点在于——`.so` 在 ZIP 里只是一段字节，要让它能被执行，必须先把它「伪装」成一个可被动态链接器接受的文件。OM2 的做法是 `memfd_create` 在内存里造一个匿名文件、把 so 字节写进去、再用 `/proc/<pid>/fd/<fd>` 路径去 `dlopen`，全程不落盘。

加载完成后，对模型的每一次执行都只是「把输入输出指针传给 `run_func`」——因为模型逻辑已经是编译好的原生代码了。

#### 4.2.2 核心流程

`Om2ModelExecutor::Load` 的完整链路（从 `.om2` 文件到可 Run）：

```text
LoadOm2DataFromFile(path)        # 1. 把整个 .om2 读进内存 buffer
  └─ LoadOm2ExecutorFromData      # 2. new Om2ModelExecutor + Load
       └─ Load(ModelData&)        # 3. 两个重载：先解 ZIP，再走结构化 Load
            ├─ RAIIZipArchive(buffer)        # 用内存 ZIP 读取器打开
            ├─ DeserializeOm2ModelDataFromArchive  # 遍历 entry → Om2ModelData
            └─ Load(Om2ModelData&)           # 4. 结构化加载（真正干活）
                 ├─ ValidateVarMetas                      # 校验 var_meta index
                 ├─ Impl::LoadFromOm2ModelData            # 解析 meta / kernel / ★LoadSoFromBuffer(memfd_create)
                 ├─ Impl::LoadSharedObject                # ★mmDlopen(so)
                 ├─ Impl::ResolveSymbols                  # ★mmDlsym 五个符号 → 函数指针
                 ├─ Impl::CreateDumpManager
                 └─ Impl::CreateAndLoadModelFromStruct
                      ├─ CreateModelFromStruct
                      │    ├─ PrepareWorkPtr              # 分配/复用 work 显存
                      │    ├─ PrepareConstantsFromStruct  # 常量分类 + H2D
                      │    ├─ (若 rt_var_resource) VarMgr.Init / TransAllVarData / CopyVarData  # 见 4.3
                      │    ├─ PrepareVarAddrs             # 取每个 var 的设备地址
                      │    └─ ★create_func(...)           # 调用 Om2ModelCreate → 得到 model_handle
                      └─ LoadModel → ★load_func(...)      # 调用 Om2ModelLoad
```

执行阶段则非常薄：

```text
Run(inputs, outputs)      → run_func(...)        # Om2ModelRun，同步
RunAsync(stream, in, out) → run_async_func(...)  # Om2ModelRunAsync，异步（带 stream）
```

#### 4.2.3 源码精读

**① 五个函数指针的类型契约**——这是 host 端对 generated so 的「接口定义」。运行时严格按这五个 `using` 转换 `dlsym` 返回的裸指针：

[runtime/om2/om2_model_executor.cc:50-55](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L50-L55) 定义 `CreateFunc / LoadFunc / DestroyFunc / RunFunc / RunAsyncFunc`。注意 `CreateFunc` 的长参数表与 codegen 端 `Om2ModelCreate` 声明一一对应——两端必须完全吻合，否则 `reinterpret_cast` 后调用即未定义行为。承载这些指针与 so 句柄的结构是 `RunModelInfo`（[57-71](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L57-L71)）：`so_handle`、`model_handle`、`rt_model_handle`、五个 `*_func`。

**② 把 so 字节变成可 dlopen 的「文件」（memfd_create）**——这是最巧妙的一步。`CreateSoMemFd` 用系统调用 `memfd_create` 在内存中造一个匿名文件，写入 so 字节，再返回 `/proc/<pid>/fd/<fd>` 路径：

[runtime/om2/om2_model_executor.cc:121-141](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L121-L141)：`syscall(__NR_memfd_create, ...)` 创建 fd，`mmWrite` 写入 so 数据，`lseek` 回头，最后 `fd_path = "/proc/" + pid + "/fd/" + fd`。这样既不落盘（安全、无 IO），又能被动态链接器按普通 so 加载。`Impl::LoadSoFromBuffer`（[821-829](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L821-L829)）就是它的薄封装。

**③ dlopen + dlsym 解析符号**：

[runtime/om2/om2_model_executor.cc:832-844](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L832-L844) `LoadSharedObject` 用 `mmDlopen(path, MMPA_RTLD_NOW)` 打开 so（`RTLD_NOW` 表示立即解析所有符号，有问题立刻报错）。[846-860](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L846-L860) `ResolveSymbols` 用 `mmDlsym` 依次取 `"Om2ModelCreate" / "Om2ModelLoad" / "Om2ModelDestroy" / "Om2ModelRun" / "Om2ModelRunAsync"`，每个都 `GE_ASSERT_NOTNULL`——任何一个符号缺失都直接失败。

**④ CreateModelFromStruct——凑齐弹药并调用 Om2ModelCreate**：

[runtime/om2/om2_model_executor.cc:901-941](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L901-L941)：先 `PrepareWorkPtr`（[1326-1341](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1326-L1341)）分配设备 work 显存（或复用外部传入的 `work_ptr`），再 `PrepareConstantsFromStruct` 准备常量指针数组，处理 Variable（见 4.3），`PrepareVarAddrs` 收集变量地址，最后在第 [935-939](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L935-L939) 行调用 `create_func(...)` 把 bin 文件、常量、变量地址、work_ptr、session_id、model_id、dump_manager 一并交给 generated so，换回 `model_handle` 与 `rt_model_handle`。随后 `CreateAndLoadModelFromStruct`（[943-958](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L943-L958)）调用 `LoadModel()`（[1013-1018](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1013-L1018)，即 `load_func` → `Om2ModelLoad`）完成 so 内部的资源初始化（注册 kernel、建流/事件等）。

**⑤ Run / RunAsync——极薄的执行层**：

[runtime/om2/om2_model_executor.cc:1030-1050](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1030-L1050) `Run`：读取流同步超时，按需构造 `Om2ProfInfos`（profiling 开启时才传非空），把输入输出 `Tensor*` 数组 reinterpret 成 `void**` 后调用 `run_func`（`Om2ModelRun`），完成后上报模型级 profiling、`step_id_++`。[1052-1072](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1052-L1072) `RunAsync` 同理，但多传一个 `stream`，走 `run_async_func`（`Om2ModelRunAsync`）。

**⑥ 进程级管理器 Om2ModelManager**——对应 v1 的 ModelManager，按 model_id 存 `shared_ptr<Om2ModelExecutor>`：

[runtime/om2/om2_model_manager.cc:24-45](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_manager.cc#L24-L45) `LoadModel` 校验 id 不重复后 new 一个 executor 并 `Load`；[47-69](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_manager.cc#L47-L69) `RunModel` 按 `stream == nullptr` 二选一：同步 `Run` 或异步 `RunAsync`。

**⑦ 生命周期收尾**——`Cleanup` 对称释放：

[runtime/om2/om2_model_executor.cc:1301-1323](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1301-L1323)：先调 `destroy_func`（`Om2ModelDestroy`）释放 so 内部资源，再 `mmDlclose`、`CloseMemFd`（关掉 memfd），最后 `ReleaseOwnedMemory`（[1407-1414](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1407-L1414)）逐个 `aclrtFree` 掉自己分配的 work/weight 显存。析构函数（[1439-1443](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1439-L1443)）自动触发 Cleanup。

**补充：外部内存与零拷贝。** [Om2ModelLoadArg](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/inc/framework/runtime/om2_model_executor.h#L24-L37) 允许调用方传入 `work_ptr/weight_ptr`（外部显存）与 `reuse_zero_copy`。`PrepareWorkPtr` 在 `reuse_zero_copy=true` 时会从 work_size 中扣掉 `zero_copy_size`（[1326-1341](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1326-L1341)），实现用户输入输出与模型内存的直接对接，避免额外拷贝。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：跟踪一次 OM2 模型从文件到 Run 的完整调用链，亲手把「函数指针」的每一步对上。

**操作步骤**：

1. 阅读 ST 测试 [tests/ge/st/testcase/test_om2.cc:2248-2269](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc#L2248-L2269)（`LoadGeneratedOm2_Ok_ExecutorMainFlow`）：它依次调用 `LoadOm2DataFromFile` → `IsOm2Model`/`GetOm2MemAndWeightSize`（[2194-2213](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc#L2194-L2213)）→ `LoadOm2ExecutorFromData` → `GetModelDescInfo` → `Run` / `RunAsync`（[2238-2246](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc#L2238-L2246)）。
2. 对照 [om2_model_executor.cc:1445-1472](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1445-L1472) 的两个 `Load` 重载，确认 `LoadOm2ExecutorFromData`（[1597-1609](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1597-L1609)）内部正是先 `ResolveSessionId` 再 `executor->Load`。
3. 在源码里用搜索定位五个 `*_func` 的赋值点（`ResolveSymbols`）和调用点（`create_func` 在 `CreateModelFromStruct`、`load_func` 在 `LoadModel`、`run_func`/`run_async_func` 在 `Run`/`RunAsync`、`destroy_func` 在 `Cleanup`），画出「符号名 → 赋值处 → 调用处」的对照表。

**需要观察的现象 / 预期结果**：你会看到这五个指针构成了模型的全部对外行为——加载之外的查询接口（`GetModelDescInfo` 等）其实读的是 `model_meta_info_`（[73-85](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L73-L85)），并不经过 so；只有 Create/Load/Run/Destroy 才真正进入 generated so。「待本地验证」：在有昇腾设备的环境编译并运行该 ST，观察日志里 `[OM2] Begin loading so file ...` 与 `Om2ModelLoad` 是否如期出现。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `memfd_create` + `/proc/<pid>/fd/<fd>` 而不是把 so 写到 `/tmp` 再 dlopen？

**参考答案**：避免落盘带来的 IO 开销、磁盘空间占用、并发冲突与残留文件清理问题，也避免模型 so 明文落盘后被拷走；memfd 全程在内存中，生命周期与 fd 绑定，`CloseMemFd` 即自动回收。代价是只在支持 `memfd_create` 的 Linux 内核上可用。

**练习 2**：`mmDlopen` 用了 `MMPA_RTLD_NOW`，如果改成 lazy binding（`RTLD_LAZY`）会对 OM2 加载有什么影响？

**参考答案**：`RTLD_NOW` 在 dlopen 时立即解析全部符号，配合紧随其后的 `ResolveSymbols`（对五个符号 `ASSERT_NOTNULL`），能保证「so 缺符号」在加载阶段就立即暴露，而不是拖到 Run 时才崩溃；改成 lazy 后，缺失的符号可能直到运行期首次调用才触发错误，排查更困难。

**练习 3**：`Om2ModelManager::RunModel` 如何用同一个入口区分同步与异步执行？

**参考答案**：见 [om2_model_manager.cc:65-68](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_manager.cc#L65-L68)：`stream == nullptr` 时调 `executor->Run`（同步，内部走 `run_func` / `Om2ModelRun`），否则调 `executor->RunAsync(stream, ...)`（异步，走 `run_async_func` / `Om2ModelRunAsync`）。调用方是否传 stream 即同步/异步的开关。

---

### 4.3 Om2RTVarManager 运行时变量管理

#### 4.3.1 概念说明

Variable（变量）是模型里一类特殊的「参数张量」——推理时它的值来自外部加载的权重文件，而不是用户每次推理传入的输入。OM 模型里 Variable 通常在加载阶段就被赋值；OM2 里这件事由 `Om2RTVarManager`（Runtime Variable Manager）专门负责。

它要回答三个问题：

1. **地址**：每个 Variable 在设备上的地址从哪来？是现分配、还是用用户给的外部地址、还是映射进一块预分配的「变量大池子」？
2. **数据**：地址有了，权重数据怎么搬进去？是直接从归档里内嵌的初始字节拷贝，还是从「老格式」的变量转换过来，还是从另一个变量拷贝过来？
3. **并发**：成百上千个 Variable 的转换能否并行？

注意类名里的 `RT`（Runtime）——它取代了此前的 `om2_var_manager`，是本次代码演进中新落地的运行时变量管理器，并通过 `Om2RTVarManagerPool` 按 **session_id** 隔离不同会话的变量空间。

#### 4.3.2 核心流程

变量管理的总流程（在 `CreateModelFromStruct` 里被串联）：

```text
if (model_data.rt_var_resource != nullptr):
    var_manager = Om2RTVarManagerPool.GetManager(session_id)
    var_manager.Init(*rt_var_resource)            # 登记所有变量条目
    var_manager.TransAllVarData(var_names, dev, graph_id)  # 路径②：格式/类型转换（多线程）
    var_manager.CopyVarData(var_names, dev)        # 路径③：变量间拷贝
PrepareVarAddrs(...)                               # 对每个 var 调 GetVarDevAddr → var_addrs[]
create_func(..., var_addrs, ...)                   # 把地址数组交给 generated so
```

**设备地址的解析优先级**（`GetVarDevAddr` 内部，三者择一）：

\[
\text{dev\_addr} =
\begin{cases}
\text{entry.extern\_dev\_addr}, & \text{用户已提供设备地址} \\
\text{external\_arena} + (\text{logic\_addr} - \text{base}), & \text{提供了外部变量大池} \\
\text{Om2Malloc}(\text{size}), & \text{否则现分配}
\end{cases}
\]

其中外部池基址 `base = kMemoryVarLogicBase = 128\text{GB}`（[om2_rt_var_manager.h:26](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.h#L26)），编译期为每个变量分配的逻辑地址都从这个基址起算，运行时用「逻辑地址 − 基址」得到池内偏移。

**数据搬运的三条路径**：

| 路径 | 触发条件 | 数据来源 | 关键操作 |
| --- | --- | --- | --- |
| ① init_data | entry 自带初始字节 | 归档 `var_weight_data` 切片 | 首次 `GetVarDevAddr` 时 `aclrtMemcpy` H2D |
| ② 转换 (trans_road) | 变量在图间格式/dtype 变化（`changed_graph_id != allocated_graph_id`） | 另一个「老格式」变量 | D2H 取回 → Host 上 TransDataFormat/CAST → H2D 写新址 |
| ③ 拷贝 (copy_info) | 变量声明复制自另一变量 | `copy_info.src_var_name` 指向的源变量 | D2H 取源 → 可选 dtype cast → H2D 写目标 |

#### 4.3.3 源码精读

**① Variable 条目与集合**——`RTVarEntry` 是单个变量的完整描述：

[base/common/om2/rt_var_resource.h:39-53](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/rt_var_resource.h#L39-L53)：含 `var_name`/`var_key`、`logic_addr`/`size`/`memory_type`、`tensor_desc`、`trans_road`（转换路径）、`copy_info`、`extern_dev_addr`、`init_data`（内嵌初始字节）。`RTVarResource`（[55-67](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/rt_var_resource.h#L55-L67)）维护 `var_key → entry` 表与 `name → key` 反查。

**② session 级管理池**：

[runtime/om2/om2_rt_var_manager.h:78-90](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.h#L78-L90) `Om2RTVarManagerPool` 是单例，`GetManager(session_id)` 懒创建（[om2_rt_var_manager.cc:525-536](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L525-L536)）。不同 session 的变量互不影响，`RemoveManager` / `Destroy` 时统一 `Finalize` 释放显存。

**③ Init——登记条目**：

[runtime/om2/om2_rt_var_manager.cc:51-62](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L51-L62)：把 `RTVarResource` 里的所有 entry 按 `var_key` 去重后并入 `var_resource_`，同时记下可选的外部变量大池地址。`Om2RTVarManager` 内部用 `recursive_mutex` 保护，因为转换流程会递归调用自身的 `GetVarDevAddr`。

**④ GetVarDevAddr——地址解析 + 路径①（init_data 搬运）**——这是变量管理的核心：

[runtime/om2/om2_rt_var_manager.cc:97-147](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L97-L147)：
- 先查 `state.dev_addrs[device_id]` 缓存，命中直接返回（每个变量按设备缓存地址）。
- 否则按前述优先级选地址：`extern_dev_addr` → 外部池偏移（越界校验 `offset + size <= external_var_size_`，[110-122](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L110-L122)）→ `AllocDevAddr`（`Om2Malloc` → `aclrtMalloc`，[64-74](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L64-L74)）。
- 地址记入 `state.dev_addrs` 后，若 `init_data` 非空且该设备尚未加载（[131-144](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L131-L144)），用 `aclrtMemcpy(..., ACL_MEMCPY_HOST_TO_DEVICE)` 把初始字节灌进设备，并置 `is_loaded[device_id]=true`。这就是**路径①**——最常见、最便宜的加载方式。

**⑤ TransAllVarData——路径②（格式/类型转换，多线程）**：

[runtime/om2/om2_rt_var_manager.cc:379-444](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L379-L444)：先筛选真正需要转换的变量——`trans_road` 非空、`changed_graph_id` 与当前图一致且不同于分配图、尚未加载、且 `NeedRealTrans`（排除纯 RESHAPE/REFORMAT/SQUEEZEV2/UNSQUEEZEV2 这类无需实算的节点，[37-44](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L37-L44)）。然后用一个 16 线程的 `ThreadPool`（`kDefaultVarTransThreadNum`，[30](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L30)）并发提交 `TransSingleVarData`，每个任务先 `aclrtSetCurrentContext` 再转换。

单个变量的转换 `TransSingleVarData`（[341-377](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L341-L377)）：`CopyVarFromDevice`（D2H 取老变量）→ `TransVarOnHost`（Host 上按 `trans_road` 逐节点变换）→ `GetVarDevAddr`（拿到/分配新址）→ `CopyVarToDevice`（H2D 写新址）。其中 `TransVarOnHost`（[275-339](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L275-L339)）对 `TRANSDATA/TRANSPOSED` 调 `ge::formats::TransDataFormat`、对 `CAST` 调 `TransTensorDataType`。这些格式转换函数就是下一篇（u6-l7）要展开的 OM2 FormatTransfer 子系统。

**⑥ CopyVarData——路径③（变量间拷贝）**：

[runtime/om2/om2_rt_var_manager.cc:446-514](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L446-L514)：对每个带 `copy_info.src_var_name` 的变量，定位源变量 → D2H 取源数据 → 若 dtype 不同做 `TransTensorDataType` → 取目标地址 → H2D 写入。这条路径服务于「一个变量复用另一个变量的值」的场景。

**⑦ PrepareVarAddrs——把地址数组交给 so**：

[runtime/om2/om2_model_executor.cc:885-899](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L885-L899)：按 `var_metas` 顺序，对每个变量调 `var_manager->GetVarDevAddr(var_name, device_id, dev_addr)`，按 `meta.index` 填进 `var_addrs[]`。这个数组随后在 `create_func` 调用里传给 generated so（[935-939](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L935-L939)）——so 内部据此把每个 Variable 算子的输入指针指到正确地址。

**⑧ Finalize——对称释放**：

[runtime/om2/om2_rt_var_manager.cc:153-179](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L153-L179)：遍历所有 `var_runtime_states_`，对每个已分配地址 `aclrtFree`，但**跳过 `extern_dev_addr`**（因为它不是本管理器分配的，所有权在外部）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：搞清「变量的设备地址是何时分配、数据如何搬运到设备」这两个问题。

**操作步骤**：

1. 在 [om2_model_executor.cc:920-932](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L920-L932) 确认变量初始化的三步顺序：`Init` → `TransAllVarData` → `CopyVarData`，并思考为什么必须是这个顺序（提示：转换依赖老变量已就绪，拷贝依赖源变量已就绪）。
2. 在 [om2_rt_var_manager.cc:97-147](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L97-L147) 跟踪一次「带 init_data 的普通变量」的地址分配：进入 `GetVarDevAddr` → 没缓存 → 没有 extern/外部池 → `AllocDevAddr`（`aclrtMalloc`）→ 回填 `state.dev_addrs` → 因 `init_data` 非空且未加载 → `aclrtMemcpy` H2D。
3. 对比路径②：阅读 [om2_rt_var_manager.cc:341-377](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L341-L377)，数一下一次「转换」涉及几次 Device↔Host 拷贝。

**需要观察的现象 / 预期结果**：你会确认——**地址分配发生在「首次被 `GetVarDevAddr` 查询时」**（懒分配，按设备缓存），而**数据搬运发生在同一函数内（路径①）或紧随其后的 Trans/Copy 流程（路径②③）**。一次转换涉及 2 次跨芯片拷贝（D2H 取老值、H2D 写新值），格式变换本身在 Host 上完成，所以变量越多越值得用 16 线程池并行。「待本地验证」：带 Variable 的 OM2 模型加载时，日志应出现 `[OM2][Var][Trans] var=... trans completed on device=...`。

#### 4.3.5 小练习与答案

**练习 1**：三个变量 A/B/C：A 自带 init_data，B 需要从 A 的格式转换而来（trans_road），C 声明 copy 自 B。它们的就绪顺序应如何排列？

**参考答案**：A 先就绪（路径①在 `GetVarDevAddr` 内完成 init_data 搬运）→ B 经 `TransAllVarData` 从 A 转换就绪（路径②）→ C 经 `CopyVarData` 从 B 拷贝就绪（路径③）。源码中 `CreateModelFromStruct` 严格按 `Init → TransAllVarData → CopyVarData` 顺序调用，正是为了满足这个依赖。

**练习 2**：`Finalize` 在释放设备地址时为什么要跳过 `entry->extern_dev_addr`？

**参考答案**：`extern_dev_addr` 是调用方（外部）传入并持有所有权的设备地址，管理器只是「借用」它当变量地址，并没有分配权；若也 `aclrtFree` 它，会double-free 外部内存。同理 `GetVarDevAddr` 中外部池偏移地址也不该被这里释放（它们落在外部池连续区间内，统一由外部释放）。`Finalize` 通过 `addr != entry->extern_dev_addr` 判断只释放自己 `AllocDevAddr` 出来的地址。

**练习 3**：`TransAllVarData` 用了 16 线程池，但每个任务第一行都是 `aclrtSetCurrentContext(context)`，为什么？

**参考答案**：线程池的工作线程默认没有绑定 ACL context，而 `aclrtMemcpy` 等 runtime 调用依赖「当前 context」才知道操作哪张卡。主线程在提交前先 `aclrtGetCurrentContext(&context)` 取到加载上下文，每个子任务进入后第一件事就是 `aclrtSetCurrentContext(context)` 把自己切到同一上下文，否则后续 D2H/H2D 拷贝会因无有效 context 而失败。

---

### 4.4 OM2 与 v1/v2 的对比（贯穿性总结）

为把三讲（u6-l1、u6-l4、本讲）串起来，这里给出一张总对比表。它不引入新源码，而是把本讲的 OM2 放回运行时全景：

| 维度 | v1（OM / DavinciModel） | v2（Lowering / ExecuteGraph） | OM2（codegen / so） |
| --- | --- | --- | --- |
| 模型产物 | 二进制 OM（protobuf TaskDef 序列化） | 运行期 Lowering 产物（非离线文件为主） | ZIP 归档：编译出的 so + json + 权重 + kernel |
| 模型逻辑载体 | 运行时解释的任务清单 | Lowering 生成的 ExecuteGraph | **编译好的原生 C++ 代码（so）** |
| 加载方式 | 反序列化 → DavinciModel 六阶段 | ComputeGraph → ExecuteGraph 转换 | dlopen(so) + dlsym 五符号 + 函数指针调用 |
| 执行模型 | `rtModelExecute` 硬件 Sink | Host 顺序/拓扑/多线程执行 | 调用 so 的 `Om2ModelRun`/`RunAsync` |
| shape 特性 | 静态 shape 为主 | 动态 shape | 编译期确定结构，支持动态档位（dynamic_batch_info） |
| 变量管理 | VariableManager（v1 体系） | rt_session 内管理 | `Om2RTVarManager`（按 session 隔离） |

一句话概括差异：**v1 是「解释数据」，v2 是「运行期再组织图」，OM2 是「直接执行编译出来的程序」**。OM2 把编译器对图的全部静态知识固化进了原生代码，从而把运行时开销降到最低，同时仍保留动态档位、零拷贝、变量加载等运行期灵活性。

## 5. 综合实践

**任务**：以「OM2 模型加载执行」为主线，画一张完整的时序图，把本讲三个模块的知识串起来。

**要求**：

1. 从用户调用 `LoadOm2ExecutorFromData(model_path, ...)` 开始，到 `executor->Run(inputs, outputs)` 返回结束。
2. 在图上至少标出以下关键节点，并注明对应的源码位置：
   - ZIP 魔数判定（`IsOm2Model`）与归档反序列化（`HandleArchiveEntry` 的路径分派）。
   - so 的 memfd_create → dlopen → dlsym（五个符号）。
   - `Om2ModelCreate` 的入参装配：work_ptr、constants、**var_addrs**。
   - `Om2RTVarManager` 的 Init → TransAllVarData → CopyVarData → PrepareVarAddrs。
   - `Om2ModelLoad` 与 `Om2ModelRun` 两次函数指针调用。
3. 在图侧用三种颜色/标记区分：① 纯 Host 内存操作 ② 涉及 Device 显存分配/拷贝的操作 ③ 进入 generated so 的调用边界。

**操作步骤**：

1. 先通读 [om2_model_executor.cc:1460-1472](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L1460-L1472)（`Load` 主干）与 [901-941](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_model_executor.cc#L901-L941)（`CreateModelFromStruct`）。
2. 把每个 `GE_ASSERT_SUCCESS(...)` 调用当成时序图上的一个框，按调用先后横向铺开。
3. 对每个框标注它属于上面三类中的哪一类。

**预期结果**：你会得到一张「中间细、两头粗」的图——两头（反序列化、Run）基本是 Host 操作，中间（CreateModelFromStruct）集中了所有 Device 交互与 generated so 调用。这张图也就是 OM2 执行器的「心智模型」。完成此图后，你应当能不查源码回答：「变量地址在哪一步交给 so？」「so 是什么时候被 dlopen 的？」「Run 时还有没有 Device 内存分配？」（答案：PrepareVarAddrs→create_func；LoadSharedObject 在 ResolveSymbols 之前；Run 不再分配 Device 内存，只传指针。）

## 6. 本讲小结

- **OM2 是「程序」而非「数据」**：编译器用 codegen 把整张模型生成成 C++、编成 `lib<模型名>_om2.so`，再和 json/权重/kernel 一起打包成 ZIP（魔数复用 `PK\x03\x04`）。
- **ZIP 按「路径前缀+后缀」分派**：`/runtime/*.so`、`model_meta.json`、`data/constants/`、`data/variables/`、`data/kernels_*.o`、`/debug/` 各自反序列化到 `Om2ModelData` 的不同字段。
- **加载链路 = memfd_create → dlopen → dlsym**：so 字节经 `memfd_create` 造匿名文件、`/proc/<pid>/fd/<fd>` 路径被 `mmDlopen`，再 `mmDlsym` 出 `Om2ModelCreate/Load/Run/RunAsync/Destroy` 五个函数指针，构成 host↔so 唯一契约。
- **执行极薄**：Create 凑齐 work_ptr/constants/var_addrs 调一次、Load 调一次、之后每次推理只调 `run_func`/`run_async_func`；`Om2ModelManager` 按 model_id 管理实例，`stream==nullptr` 决定同步/异步。
- **Om2RTVarManager 管 Variable 全生命周期**：按 session_id 隔离，`GetVarDevAddr` 按优先级（extern_dev_addr → 外部池偏移 → 现分配）懒分配地址，数据搬运分三条路径（init_data 内嵌、trans_road 转换、copy_info 拷贝），转换用 16 线程池并行。
- **OM2 vs v1/v2**：v1 解释 OM 数据、v2 运行期 Lowering、OM2 直接执行编译出的原生代码——把静态知识固化进机器码，运行时开销最低。

## 7. 下一步学习建议

- **紧接下一篇 u6-l7《OM2 格式转换子系统》**：本讲在路径②（`TransVarOnHost`）里提到了 `ge::formats::TransDataFormat` / `TransTensorDataType`，下一篇正是展开 `runtime/om2/formats` 下 NCHW/NHWC/NC1HWC0/FractalZ 等格式互转与转换器注册机制，建议紧接着读。
- **回看编译侧**：若想深入「so 是怎么被生成出来的」，可阅读 [base/common/om2/codegen/om2_codegen_model_builder.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/base/common/om2/codegen/om2_codegen_model_builder.cc) 与 `program_generator.cc`、`task_code_builder/`（本讲只覆盖了入口 `Om2CodegenAndCompile`）。
- **动手验证**：在有昇腾设备的环境编译并运行 [tests/ge/st/testcase/test_om2.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/tests/ge/st/testcase/test_om2.cc) 的 `LoadGeneratedOm2_Ok_ExecutorMainFlow`，对照本讲时序图观察日志，把「待本地验证」的结论逐一坐实。
- **横向对比**：结合 u6-l2（v1 DavinciModel）与 u6-l4（v2 Lowering）的讲义，重读本讲 4.4 的对比表，建立 GE 运行时「三条执行路径并存」的完整心智模型。
