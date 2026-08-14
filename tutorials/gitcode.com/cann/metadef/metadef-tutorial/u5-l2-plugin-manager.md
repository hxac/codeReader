# 插件管理：动态库加载与符号解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PluginManager` 的三阶段工作流程：**路径搜索 → dlopen 加载 → 符号解析**，并写出每个阶段的入口函数名。
2. 理解每个阶段「失败继续、不中断整体流程」的错误处理哲学，以及三重加载限额（数量、单库大小、累计大小）的防护意义。
3. 描述 `PluginManager` 与 u4-l5 讲过的 opp so 加载链路的复用关系：两者共用 mmpa 封装的 `mmDlopen/mmDlsym/mmDlclose` 与「dlopen 瞬间静态对象完成注册」的机制。
4. 能读懂 `plugin_manager_unittest.cc` 中的测试技巧（mmpa stub、system 造目录、环境变量注入）。

## 2. 前置知识

- **动态库（shared object，so）**：Linux 下可被多个进程共享链接的二进制。`dlopen` 把 so 装入当前进程地址空间，`dlsym` 按名字查符号（函数/变量地址），`dlclose` 卸载。metadef 不直接调用这三个系统调用，而是通过 **mmpa**（跨平台内存与进程适配层，`mmpa/mmpa_api.h`）封装的 `mmDlopen/mmDlsym/mmDlclose/mmRealPath/mmScandir`，使同一份代码可在不同操作系统上编译。
- **静态对象构造期注册**：u4-l3/u4-l4 已建立的概念——so 被 `dlopen` 的瞬间，其内部所有全局/静态对象执行构造函数，注册逻辑就在此刻完成，**不需要显式调用任何 Init 函数**。
- **RAII 与句柄生命周期**：`PluginManager` 用 `std::map<std::string, void *> handles_` 保存「so 文件名 → dlopen 句柄」，析构时统一 `mmDlclose`，这是典型的 RAII 资源管理。
- **stub 桩机制**：u1-l2 已建立的概念——单测时把依赖的真实系统库替换为假实现（`tests/depends/mmpa/src/mmpa_stub.h`），使测试脱离真实昇腾环境也能跑。
- **ge::Status**：metadef 统一的状态返回类型，`ge::SUCCESS`/`ge::FAILED`，从不抛异常。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/common/plugin/plugin_manager.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/plugin/plugin_manager.h) | `PluginManager` 类声明：路径搜索静态工具 + 加载/符号解析成员函数 + 句柄持有。注意它位于 `inc/common/`（内部接口），不在 `inc/external/`（对外 ABI 契约） |
| [base/common/plugin/plugin_manager.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc) | 全部实现：约 1200 行，覆盖 opp 路径解析、版本校验、目录扫描、dlopen、dlsym |
| [tests/ut/base/testcase/plugin_manager_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc) | 单元测试：用 `system()` 现场造目录树与 config.ini，用 mmpa stub 模拟扫描失败 |

与 u4-l5 的关系：u4-l5 讲的是**算子实现 so 专属的发现链路**（`OppSoManager` 按 opp 包版本目录搜索、`AddSoToRegistry` 按 so 内容去重后 dlopen 并把注册表拷出）。本讲的 `PluginManager` 是更通用的底座：它不关心 so 里装的是算子还是图优化 pass，只提供「给我一个路径串或目录，我把里面所有合法 so 装进来并按需校验符号」。两条链路各自调用 mmpa 的 dlopen 封装，共享「加载即注册」的时序约定。

## 4. 核心概念与源码讲解

### 4.1 PluginManager 类全景与生命周期管理

#### 4.1.1 概念说明

`PluginManager` 是 metadef 内部的通用插件装载器。它解决的问题是：图编译、算子发现、自定义融合 pass 等大量功能都以 so 形式交付，需要一个统一组件负责「找到 so 在哪、安全地装进进程、确认 so 里有需要的符号、进程退出前统一卸载」。

它有两类接口，分工明确：

- **静态工具函数**（路径搜索阶段）：`GetOppPath`、`GetOpsProtoPath`、`GetOppPluginPathOld/New` 等，不依赖任何实例状态，输入输出都是字符串。
- **成员函数**（加载与符号阶段）：`LoadSo`/`Load` 系列，加载结果存进实例成员 `so_list_`（文件名列表）与 `handles_`（文件名→句柄映射）。

#### 4.1.2 核心流程

```text
构造 PluginManager 实例
        │
        ▼
[阶段1] 静态函数拼出冒号分隔的路径串（如 "dir1:dir2:dir3"）
        │
        ▼
[阶段2] LoadSo(path) / Load(dir)：逐个 so 验大小 → mmDlopen
        │         成功 → handles_[文件名] = 句柄
        │         失败 → 打日志，continue（跳过该 so，继续下一个）
        ▼
[阶段3] 对每个已加载 so，用 mmDlsym 校验 func_check_list 中的符号
        │         缺符号 → mmDlclose 丢弃该 so，continue
        ▼
析构（或再次 Load 前）→ ClearHandles_() 统一 mmDlclose
```

#### 4.1.3 源码精读

类声明与两个核心成员（文件名列表 + 句柄表）在头文件中：

[inc/common/plugin/plugin_manager.h:L140-L142](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/plugin/plugin_manager.h#L140-L142) —— `so_list_` 记录已成功加载的 so 文件名（供日志与上层查询），`handles_` 是文件名到 `void *` 句柄的映射，是生命周期管理的核心数据。

析构时统一关闭所有句柄：

[base/common/plugin/plugin_manager.cc:L142-L155](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L142-L155) —— `ClearHandles_()` 遍历 `handles_` 逐个 `mmDlclose`，关闭失败只打告警不抛错；析构函数直接调用它。另注意：每次 `LoadSoWithFlags`/`LoadWithFlags` 开头也会先调用 `ClearHandles_()`（见 L682/L813），即**同一个实例重复 Load 会先卸载上一批 so**，实例不累积句柄。

头文件里还有一个精巧的「反查 so 路径」工具：

[inc/common/plugin/plugin_manager.h:L145-L180](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/common/plugin/plugin_manager.h#L145-L180) —— `GetSoRealPathByAddr` 用 `mmDladdr` 从任意函数指针反查出它所属 so 的磁盘路径（受限长校验与 realpath 归一化）；`GetModelPath` 则把**当前头文件自己所在 so** 的目录返回。实现侧大量用它做「以我为准」的相对路径推算（下文 `GetRequiredOppAbiVersion` 会看到）。`GetModelPath` 的技巧是把 `reinterpret_cast<void *>(&GetModelPath)` 这个**inline 函数自身的地址**传给 `GetModelPathByAddr`——inline 函数最终被编进某个 so，其地址就锚定了那个 so。

#### 4.1.4 代码实践

1. **实践目标**：确认 `PluginManager` 实例的可复用语义与句柄清理时机。
2. **操作步骤**：阅读 `plugin_manager_unittest.cc` 中的 `test_plugin_manager_load` 测试，观察它如何先手动向 `handles_` 塞入一个空句柄再调用 `ClearHandles_()`。
3. **需要观察的现象**：`manager.handles_.size() == 0` 断言通过，说明清理是全量的。
4. **预期结果**：理解「Load 前先清空」意味着一个 `PluginManager` 实例同一时刻只持有一批 so；上层若要同时持有多个包的 so，需要多个实例。待本地验证（可在 ut 环境打印 `handles_` 内容）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `handles_` 的 key 用「so 文件名」而不是完整路径？

**答案**：文件名（如 `libcust_opproto.so`）是上层调用方已知的逻辑标识，便于按名字查找与去重；完整路径在加载前经过 realpath 归一化，同一 so 可能有多条符号链接路径指向它，用文件名做 key 语义更稳定。同目录下同名 so 本就不该重复出现。

**练习 2**：`ClearHandles_()` 标记为 `noexcept`，但 `mmDlclose` 可能失败，为什么这样设计是安全的？

**答案**：`ClearHandles_()` 内部对失败只做 `GELOGW` 打日志，不会向外传播异常，函数确实不会抛出；析构函数中使用 noexcept 清理可避免析构路径上的异常逃逸（C++ 中析构抛异常是未定义行为的风险源）。

### 4.2 阶段一：so 路径搜索——从环境变量到优先级路径串

#### 4.2.1 概念说明

路径搜索阶段的产出是一个**冒号分隔的目录串**（如 `dirA:dirB:dirC`），顺序即优先级。搜索依据三个来源，优先级从高到低：

1. `ASCEND_CUSTOM_OPP_PATH` 环境变量（或经 `SetCustomOpLibPath` 注入的自定义算子库路径）；
2. `ASCEND_OPP_PATH` 下 `vendors/<厂商名>`（厂商顺序由 `vendors/config.ini` 决定）；
3. `ASCEND_OPP_PATH` 下 `built-in`（内置包永远最低）。

这套优先级直接决定了「同名算子自定义实现覆盖内置实现」的加载次序——结合 u4-l4 讲过的「先到先得、不覆盖」合并规则，先被 dlopen 的自定义算子 so 抢先注册，内置实现随后注册时不再覆盖。

#### 4.2.2 核心流程

```text
GetOpsProtoPath(opsproto_path)
  ├─ GetOppPath()            # 读 ASCEND_OPP_PATH；空则从 GetModelPath() 推算 ../ops/
  ├─ IsNewOppPathStruct()    # opp_path/built-in 目录存在 → 新目录规范
  ├─ [旧规范] GetOppPluginPathOld()
  │      # 直接拼 "<opp>/op_proto/custom/ : <opp>/op_proto/built-in/"
  └─ [新规范]
       ├─ GetPluginPathFromCustomOppPath()   # 环境变量/静态变量来源，逐个目录做版本校验
       └─ GetOppPluginPathNew()              # 解析 vendors/config.ini，逐厂商做版本校验，最后拼 built-in
```

版本校验（`IsVendorVersionValid`）决定某个厂商目录是否被拼进路径串：读取 compiler/runtime 包 `version.info` 里的 `required_opp_abi_version` 区间（如 `">=6.3,<=6.4"`），与厂商包自己的 `Version=`/`compiler_version=` 比较，不在区间内则整个目录被跳过——这是防止「旧版本算子包配新版本编译器」产生 ABI 冲突的闸门。

#### 4.2.3 源码精读

先看 opp 根路径的确定：

[base/common/plugin/plugin_manager.cc:L157-L181](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L157-L181) —— `GetOppPath` 优先读 `ASCEND_OPP_PATH` 环境变量并补尾部 `/`；环境变量为空时回退到「当前 so 所在目录向上两级再加 `ops/`」的推算。注意即使 realpath 失败也只打告警、仍返回 `ge::SUCCESS`。

新目录规范的厂商列表解析：

[base/common/plugin/plugin_manager.cc:L204-L222](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L204-L222) —— `GetOppPluginVendors` 读取 `vendors/config.ini` 的**第一行**，要求形如 `load_priority=a,b,c`：按 `=` 切成两段（段数必须恰为 2，否则 `GE_ASSERT_TRUE` 返回失败），再按逗号切出厂商名列表，最后对每个名字做 `Trim` 去空白。

自定义算子路径的注入与一个重要的工程取舍：

[base/common/plugin/plugin_manager.cc:L241-L274](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L241-L274) —— 注释解释了为什么用静态字符串 `custom_op_lib_path_`：`aclGetCustomOpLibPath` 编译在更上层的 `libregister.so` 里，而本文件在更基础的库里，**不能反向依赖**，于是由上层先调 `SetCustomOpLibPath`（或弱符号 C 接口 `SetMetadefPluginCustomOpLibPathForC`）把路径压下来。`GetPluginPathFromCustomOppPath` 逐个候选目录检查 `<path>/<sub_path>` 是否为存在的目录，再过 `IsVendorVersionValid`，合法者以 `:` 拼接。

新旧两套拼装函数：

[base/common/plugin/plugin_manager.cc:L276-L284](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L276-L284) —— `GetOppPluginPathOld`：老目录规范直接拼 `custom` 与 `built-in` 两个目录，`ReplaceFirst` 把格式串里的第一个 `%s` 替换为包名。

[base/common/plugin/plugin_manager.cc:L485-L507](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L485-L507) —— `GetOppPluginPathNew`：config.ini 解析失败时降级为只拼 `old_custom_path` + `built-in`（不报错）；成功则逐厂商过版本校验后按序拼接，`built-in` 恒排最后。

搜索阶段的总入口（带一份非常完整的目录规范文档注释）：

[base/common/plugin/plugin_manager.cc:L535-L617](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L535-L617) —— L535–L604 是中文注释块，完整描述了新老目录规范、config.ini 与环境变量两种配置方式、四种混用场景的优先级规则（新环境变量 > 新配置文件 > 老自定义 > 新内置），以及「内置算子包必须为新目录风格，否则新自定义包被忽略」的约束；L605–L617 是 `GetOpsProtoPath` 实现，按 `IsNewOppPathStruct` 分流到 Old/New 两条拼装路径。

版本校验三件套：

[base/common/plugin/plugin_manager.cc:L380-L409](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L380-L409) —— `IsVendorVersionValid(vendor_path)` 重载：路径含 `opp_latest` 时跳过校验（opp kernel 包支持独立升级）；否则读包内 `version.info` 拿 `Version=`（内置包）或 `compiler_version=`（自定义包），两者都为空时「不校验视为合法」，最终落到按版本串校验的重载。

[base/common/plugin/plugin_manager.cc:L286-L352](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L286-L352) —— `GetRequiredOppAbiVersion` 从 `GetModelPath()` 推算出的安装根目录下找 `compiler/version.info` 或 `runtime/version.info`，解析形如 `">=6.3,<=6.4,6.5"` 的区间串（`>=` 后必须紧跟 `<=`，单个版本号构成闭区间）。文件不存在或字段缺失都返回 `true`（放行），只有格式明确错误才返回 `false`。

[base/common/plugin/plugin_manager.cc:L354-L378](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L354-L378) —— `GetEffectiveVersion` 把 `6.4.T5...` 折算成可比较的整数：取前两段拼成 `64` 这种「主版本+次版本」数，次版本不足 5 位时右侧补零对齐（注释说明这是为了避免 `3.20~9.0` 之类区间取到空集）。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证优先级路径串的拼装结果与 config.ini 解析的容错性。
2. **操作步骤**：
   - 阅读 `test_GetOpsProtoPath_NewPath_PriorityOk`（[tests/ut/base/testcase/plugin_manager_unittest.cc:L371-L380](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L371-L380)）：fixture `GetNewPath` 用 `system("mkdir -p ...")` 造出 `built-in/`、`vendors/` 目录并写入 `load_priority=customize,mdc,lhisi`，再设 `ASCEND_OPP_PATH`。
   - 在本地跑：`bash tests/run_test.sh -u`，过滤 `*GetOpsProtoPath*` 用例（可用 `ctest -L ut -R GetOpsProtoPath`）。
   - 作为对照，参考 `test_plugin_manager_getopp_plugin_vendors_02/03/04`（L295–L341）三个失败用例：空文件、只有 key 没有 `=`、文件不存在，各自断言返回非 SUCCESS。
3. **需要观察的现象**：NewPath 用例断言路径串恰为 `vendors/customize/op_proto/:.../mdc/...:.../lhisi/...:built-in/op_proto/`，即 config.ini 顺序在前、built-in 恒最后。
4. **预期结果**：成功用例通过、三个非法格式用例失败（返回值非 SUCCESS），验证「解析失败即拒绝整个 config.ini」的严格语义。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`GetOppPluginVendors` 为什么要求 `v_parts.size() == 2` 而不是 `>= 2`？`load_priority=a=b,c` 会怎样？

**答案**：配置格式契约就是单行 `key=value1,value2`，按 `=` 切分若多于两段说明行内混入了第二个 `=`，属于格式错误，用 `GE_ASSERT_TRUE(v_parts.size() == kVendorConfigPartsCount)` 直接判失败（GE_ASSERT 失败会记录错误并返回失败状态），整个 config.ini 被放弃，`GetOppPluginPathNew` 走降级分支。

**练习 2**：内置算子包为什么永远排在路径串最后？

**答案**：结合 u4-l4 的「先到先得、不覆盖」注册合并规则，加载顺序即覆盖顺序。自定义算子包排前面先 dlopen、先注册，用户自定义的同名算子得以覆盖内置实现；内置排最后保证它只兜底。

**练习 3**：`custom_op_lib_path_` 为什么设计成静态成员而不是构造参数？

**答案**：见源码注释（L241–L244）：该路径的权威来源 `aclGetCustomOpLibPath` 在更上层的 `libregister.so`，`plugin_manager.cc` 所在的基础库不能反向依赖它；用进程级静态字符串让上层在初始化时「压入」路径，基础库之后只读，解除了依赖环。配套的 `SetMetadefPluginCustomOpLibPathForC` 是弱符号 C 接口，供跨 so 调用。

### 4.3 阶段二：dlopen 加载——LoadSo / Load 双通道与三重限额

#### 4.3.1 概念说明

拿到路径串后进入加载阶段。`PluginManager` 提供两条平行通道，**接口形状完全对称**：

| 通道 | 入口 | 输入含义 |
| --- | --- | --- |
| 路径串通道 | `LoadSo` → `LoadSoWithFlags` | 参数是**冒号分隔的多个具体 so 路径**（由阶段一产出） |
| 目录通道 | `Load` → `LoadWithFlags` | 参数是**一个目录**，自动扫描目录下所有 `.so` 后缀文件 |

两者都以 `RTLD_NOW | RTLD_GLOBAL` 为默认 flags：`RTLD_NOW` 立即解析所有重定位（坏 so 当场暴露而不是留到调用时崩溃），`RTLD_GLOBAL` 把符号并入全局符号表（后加载的 so 能引用先加载 so 的符号——算子 so 之间共享注册基础设施依赖这一点）。

加载阶段有三重限额防护（防恶意或损坏的算子包拖垮进程）：

- 单个路径串/目录最多加载 **64** 个 so（`kMaxNumOfSo`）；
- 单个 so 文件不超过 **800MB**（`kMaxSizeOfSo`）；
- 累计加载不超过 **1000MB**（`kMaxSizeOfLoadedSo`）。

#### 4.3.2 核心流程

以 `LoadSoWithFlags` 为例（`LoadWithFlags` 流程相同，仅多了目录扫描前奏）：

```text
for 每个 single_path in SplitPath(path, ':'):
    1. 路径长度 >= MMPA_MAX_PATH        → 打日志，continue
    2. 已加载数达到 kMaxNumOfSo(64)      → 打告警，break（跳出整个循环）
    3. RealPath(single_path) 失败/为空    → 打告警，continue
    4. ValidateSo 失败（大小超限）        → 打告警，continue
    5. handle = mmDlopen(realpath, RTLD_NOW|RTLD_GLOBAL)
       handle == nullptr                 → 打告警，continue
    6. (阶段三) func_check_list 符号校验
       缺符号 → mmDlclose(handle)，continue
    7. 累计大小 += file_size；so_list_.push_back(文件名)；
       handles_[文件名] = handle；num_of_loaded_so++
返回 ge::SUCCESS（即使一个都没加载成功也返回 SUCCESS，只打告警）
```

关键错误处理结论：**单个 so 的任何失败都是「跳过」而非「中止」**——一个损坏的厂商包不应阻断其他包加载；只有数量封顶用 `break`（继续扫描没有意义）。函数级返回值几乎恒为 `ge::SUCCESS`，「没装上任何 so」也只以告警日志表达（`No loadable shared library found`），调用方需结合日志与 `so_list_` 判断实际效果。

#### 4.3.3 源码精读

两个入口的默认 flags 转发：

[base/common/plugin/plugin_manager.cc:L671-L675](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L671-L675) —— `LoadSo` 把 `MMPA_RTLD_NOW | MMPA_RTLD_GLOBAL` 组合传给 `LoadSoWithFlags`；L800–L804 的 `Load` 对 `LoadWithFlags` 做同样的事。`flags` 经由 `static_cast<uint32_t>` 再 `static_cast<int32_t>` 的往返转换是为了避免有符号位或时的未定义行为。

路径串通道主体：

[base/common/plugin/plugin_manager.cc:L677-L756](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L677-L756) —— 循环体完整实现 4.3.2 伪代码：L686–L697 路径长度与数量封顶检查；L699–L704 取文件名与 realpath；L707–L710 大小校验；L715–L725 带耗时打点的 `mmDlopen`（`[GEPERFTRACE]` 日志记录每个 so 的加载微秒数，头文件 L32–L48 的 `InvokeFuncPerfRecorder` 是同思路的 RAII 计时器）；L752–L755 才真正记账入 `handles_`。

限额常量与校验函数：

[base/common/plugin/plugin_manager.cc:L31-L33](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L31-L33) —— `kMaxNumOfSo = 64`、`kMaxSizeOfSo = 838860800`（800MB）、`kMaxSizeOfLoadedSo = 1048576000`（1000MB）。

[base/common/plugin/plugin_manager.cc:L771-L798](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L771-L798) —— `ValidateSo` 用 `stat` 取文件大小：单文件超 800MB 拒绝；「已累计大小 + 本文件」将超 1000MB 也拒绝。返回 `ge::FAILED` 表示「这个 so 不装」，调用方 continue。

目录通道的扫描前奏：

[base/common/plugin/plugin_manager.cc:L806-L853](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L806-L853) —— `LoadWithFlags` 先对传入目录做 realpath 与 `mmIsDir` 检查（不是目录直接返回 SUCCESS）；然后 `mmScandir` 扫描，L845–L851 的过滤条件把「子目录、文件名不比 `.so` 长、后缀不是 `.so`」的条目全部跳过。注意 realpath/非目录失败返回的是 `ge::SUCCESS`（目录不存在被视为「无事发生」），`mmScandir` 返回负数才是 `ge::FAILED`。L838 之后的循环体与 `LoadSoWithFlags` 逐行同构。

另一个专用的目录扫描工具（按后缀过滤，支持 `rt.so` 这类多级后缀）：

[base/common/plugin/plugin_manager.cc:L1092-L1141](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L1092-L1141) —— `GetFileListWithSuffix` 用 `mmScandir(..., alphasort)` 按字母序枚举目录，挑出非目录且 `IsEndWith(name, so_suff)` 的文件拼全路径。u4-l5 讲过的 `IsSplitOpp` 判断（是否拆分形态 opp 包）就靠它找 `*_rt.so`。同文件 L1213–L1229 的 `GetOppPkgPath` 用它实现「子包目录有 so 就用子包路径，否则用整包路径」的二选一。

#### 4.3.4 代码实践

1. **实践目标**：验证三重限额中「单文件超 800MB 被拒」与「累计超 1000MB 被拒」两条边界。
2. **操作步骤**：阅读两个测试（它们不用真的写 800MB 数据，而是 `open + truncate` 稀疏文件）：[tests/ut/base/testcase/plugin_manager_unittest.cc:L234-L265](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L234-L265)。第一个用 `CreateSoFile("myfile.so", kMaxSizeOfSo + 1)` 造出边界外文件断言 `ValidateSo` 返回 FAILED 且 `file_size` 被回填为真实大小；第二个造 800MB-1 的文件、把 `size_of_loaded_so` 设为 `kMaxSizeOfLoadedSo - filesize + 1`（恰好累计超限 1 字节），断言 FAILED。
3. **需要观察的现象**：两条断言都通过；若把文件大小改成 `kMaxSizeOfSo - 1` 且累计不超限，`ValidateSo` 返回 SUCCESS。
4. **预期结果**：确认限额是「大于即拒绝」的开区间语义，且第二重限额是「加载前预判」而非「加载后回滚」。待本地验证（跑 `ctest -R ValidateSo`）。

#### 4.3.5 小练习与答案

**练习 1**：`LoadSoWithFlags` 在 dlopen 前先做 `RealPath` 归一化，除了拿到绝对路径还有什么安全意义？

**答案**：`RealPath`（封装自 `inc/graph/utils/file_utils.h`）会解析符号链接并要求目标真实存在且可访问。路径里可能含 `..` 或指向敏感位置的软链，归一化后再 `stat`/`dlopen`，保证校验的大小与实际加载的是同一个文件，也把「路径存在但不可解析」的坏输入提前拦截。

**练习 2**：`LoadSo("a.so:b.so", {})` 与 `Load("./dir", {})` 在「传入内容语义」上有什么本质区别？为什么需要两条通道？

**答案**：`LoadSo` 接收的是**显式枚举的 so 路径清单**（来自阶段一的优先级拼装，顺序精确可控、可跨目录挑选）；`Load` 接收的是**一个目录**，装的是目录下全部 `.so`（顺序由 scandir 决定）。前者服务于「按优先级精确装载 opp 包内指定 so」的场景（u4-l5 的 `OppSoManager` 风格），后者服务于「把某个插件目录整个挂进来」的粗粒度场景。

**练习 3**：为什么单个 so 加载失败选择 `continue` 而不是返回 FAILED？

**答案**：插件体系的核心诉求是鲁棒：一个第三方算子包损坏、缺依赖或架构不匹配，不应拖垮内置算子与其他厂商包的加载。失败信息通过告警日志（含 `mmDlerror` 的系统错误串）留给运维排查，函数返回值只表达「流程完成」而非「全部成功」。

### 4.4 阶段三：符号解析——func_check_list 校验与句柄关闭

#### 4.4.1 概念说明

dlopen 成功只说明 so 装进来了，不说明它**是调用方想要的那种 so**。比如一个装图优化 pass 的 so 被误放进算子目录，dlopen 依然成功。`func_check_list` 参数就是「类型身份证」：调用方列出该类 so 必须导出的符号名（通常是约定好的入口函数），`PluginManager` 逐个 `mmDlsym` 验证，**缺任何一个就整体丢弃该 so**（`mmDlclose` 后 continue）——注意符号校验的意图是「验证身份」，校验通过后 `PluginManager` 自己并不调用这些符号；真正的函数获取由上层（如 u4-l4 的 `GetOpImplFunctionsV2` 两步协议）通过 dlsym 完成。

#### 4.4.2 核心流程

```text
for func_name in func_check_list:
    real_fn = mmDlsym(handle, func_name)
    real_fn == nullptr:
        REPORT_INNER_ERR_MSG + GELOGE(PARAM_INVALID)   # 记录错误码 E19999
        is_valid = false; break
if !is_valid:
    mmDlclose(handle)      # 释放句柄，不留半加载状态
    continue
```

#### 4.4.3 源码精读

路径串通道的符号校验与失败回收：

[base/common/plugin/plugin_manager.cc:L729-L749](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L729-L749) —— 对 `func_check_list` 逐个 `mmDlsym`；任一符号缺失时打 `REPORT_INNER_ERR_MSG("E19999", ...)` + `GELOGE(ge::PARAM_INVALID, ...)`（这是全文件少数使用 ERROR 级别而非 WARNING 的路径，说明「装错类型的 so」比「so 损坏」更值得上报告警系统），然后 `mmDlclose` 回收句柄并 continue。目录通道的同构逻辑在 L889–L907，日志级别为 WARNING。

弱符号 C 接口（跨 so 的路径注入）：

[base/common/plugin/plugin_manager.cc:L1232-L1236](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L1232-L1236) —— `SetMetadefPluginCustomOpLibPathForC` 声明为 `extern "C" __attribute__((weak))`：弱符号保证即使上层库没提供也不断链；C 链接修饰使 `dlsym` 可按未修饰名直接找到它。它只做空指针防御后转发给 `SetCustomOpLibPath`。u4-l4/u5-l1 已建立「弱符号保障跨 so 兼容」的同款模式认知，这里是同一手法在 C 接口上的应用。

#### 4.4.4 代码实践

1. **实践目标**：观察「so 存在但缺少约定符号」时的跳过行为。
2. **操作步骤**：阅读 `test_plugin_manager_load_so_fail`（[tests/ut/base/testcase/plugin_manager_unittest.cc:L267-L274](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L267-L274)）：它拿测试依赖里真实存在的 `./tests/depends/mmpa/libmmpa.so` 来加载，但 `func_check_list` 填了不存在的 `invalid_func`。两个断言都 `EXPECT_EQ(..., ge::SUCCESS)`——**故意断言返回 SUCCESS**，验证「符号校验失败被吞掉、流程继续」。
3. **需要观察的现象**：返回值为 SUCCESS；日志中能看到 `is skipped since function invalid_func does not exist` 与随后的 dlclose。
4. **预期结果**：确认 func_check_list 失败语义是「跳过该 so 但不失败整个 Load」。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么符号缺失路径用 `GELOGE`（ERROR）而 dlopen 失败只用 `GELOGW`（WARNING）？

**答案**：dlopen 失败常见于依赖缺失、架构不匹配等环境问题，属可预期的外部异常，WARNING 足够；而「so 能装但缺少约定入口符号」意味着交付件类型放错或构建产物不完整，是交付质量问题，需要 `REPORT_INNER_ERR_MSG` 上报到错误管理（u5-l3 将展开 error_manager 体系）并打 ERROR 引导立即修复。

**练习 2**：`func_check_list` 校验通过后，调用方如何真正拿到并调用 so 里的函数？

**答案**：`PluginManager` 只存句柄不做调用。上层另起 dlsym 协议——例如 u4-l4 讲过的跨 so 两步协议：先 `dlsym` 出 `GetRegisteredOpNum` 拿数量，再 `dlsym` 出 `GetOpImplFunctionsV2` 分批把注册表拷出。`func_check_list` 相当于「预检」，正式取数协议由各业务自行定义。

**练习 3**：`mmDlclose(handle)` 在符号校验失败后被立即调用，但成功加载的 so 到析构才关闭。这种差别的原因是什么？

**答案**：校验失败的 so 对调用方毫无价值，立即关闭释放地址空间与文件描述符；成功加载的 so 里的静态注册对象（注册表条目）在进程内被全局引用，提前 dlclose 会导致这些注册失效甚至悬垂指针，因此持有到 `PluginManager` 生命周期结束（与 u4-l5「成功加载的 so 永不主动 dlclose」的策略一致，此处至迟到析构）。

### 4.5 单测解读：plugin_manager_unittest.cc 与 mmpa stub

#### 4.5.1 概念说明

这个 1100 多行的测试文件是学习「如何测试文件系统交互代码」的好样本。它有四个可复用的技巧：

1. **`system()` 现场造目录树**：fixture 与各用例直接 `mkdir -p`、`touch`、`echo ... > config.ini` 构造 opp 目录结构，TearDown 里 `rm -rf` 清理。
2. **环境变量注入用 `mmSetEnv` 而非 `setenv`**：走 mmpa 封装，与被测代码读环境变量的路径一致。
3. **mmpa stub 注入**：`ge::MmpaStub::GetInstance().SetImpl(...)` 替换 `mmScandir` 等实现，模拟「扫描失败」等难以真实构造的故障。
4. **稀疏文件**：`open + truncate` 造出 800MB 的「假 so」而几乎不占磁盘。

#### 4.5.2 核心流程

```text
TEST_F(UtestPluginManager, xxx)
  ├─ [可选] GetOldPath()/GetNewPath()   # fixture 辅助：造 opp 目录 + 设环境变量
  ├─ system("mkdir -p / touch / echo")  # 造测试场景
  ├─ 调 PluginManager 静态/成员函数
  └─ EXPECT_EQ / TearDown 清理
```

#### 4.5.3 源码精读

fixture 与目录构造：

[tests/ut/base/testcase/plugin_manager_unittest.cc:L152-L198](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L152-L198) —— `GetOldPath` 只设 `ASCEND_OPP_PATH` 不造目录（模拟旧规范路径）；`GetNewPath` 造 `built-in`、`vendors` 目录并把厂商名列表格式化成 `load_priority=n1,n2,...` 写入 config.ini；TearDown 统一 `rm -rf`。

mmpa stub 的两个替身：

[tests/ut/base/testcase/plugin_manager_unittest.cc:L72-L95](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L72-L95) —— `MockMmpa` 在真实 `scandir` 之上把目录项的 `d_type` 改写为 `DT_UNKNOWN`，验证被测代码在文件系统不提供类型信息时仍能工作（`GetOppSupportedOsAndCpuType` 的用例在 L554–L591 里先原生跑一遍、再注入 MockMmpa 跑一遍）；`MockMmpaInvalid` 直接让 `mmScandir` 返回 -1，配合 `FindSoFilesInCustomPassDirs_scan_dir_failed`（L918–L935）验证扫描失败的静默降级（`so_files` 为空、不崩溃）。

子包/整包二选一的表驱动式断言：

[tests/ut/base/testcase/plugin_manager_unittest.cc:L1099-L1130](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L1099-L1130) —— 同一个测试里依次覆盖三种状态：子包目录不存在 → 返回整包路径；子包目录存在但无合法 `.so` → 仍返回整包路径；子包目录有 `.so` → `is_sub_pkg == true` 且返回子包路径。注意 `.so1`/`.so2` 后缀的干扰文件被正确过滤。

版本校验用例的「写配置→断言→换配置」节奏：

[tests/ut/base/testcase/plugin_manager_unittest.cc:L723-L788](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L723-L788) —— `test_plugin_manager_IsVendorVersionValid` 与 `..._GetRequiredOppAbiVersion_InValid` 反复改写 `runtime/version.info` 内容（合法区间、单个版本、非法字符 `6.#`、缺 `<=` 的残缺区间），逐一断言版本判定结果；`WriteRequiredVersion` 辅助函数（L40–L52）同时在 `./runtime` 与 `./../runtime` 两处写文件，因为 `GetRequiredOppAbiVersion` 的目录推算结果取决于测试可执行文件的安装层级。

#### 4.5.4 代码实践

1. **实践目标**：体会「文件系统型单测」的构造与清理模式，并理解 `FindSoFilesInCustomPassDirs` 的层级约束。
2. **操作步骤**：
   - 精读 `FindSoFilesInCustomPassDirs_01`（[tests/ut/base/testcase/plugin_manager_unittest.cc:L937-L955](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/plugin_manager_unittest.cc#L937-L955)）：造 `vendors/1/2/custom_fusion_passes`（三层，非法）与 `vendors/2/custom_fusion_passes`（两层，合法，内放 concat_pass.so），断言只找到后者。
   - 对照实现 [base/common/plugin/plugin_manager.cc:L1143-L1174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L1143-L1174)：`FindSoFilesInCustomPassDirs` 用 `FilterDirectories` 过滤 `.`/`..` 后只扫一层子目录，再进入每个子目录的 `custom_fusion_passes` 找 `.so`（`ProcessSubdirectoryAndSoFiles`，L112–L139）。
   - 本地运行：`bash tests/run_test.sh -u` 后执行 `ctest -L ut -R FindSoFilesInCustomPassDirs`。
3. **需要观察的现象**：三层深度的 pass 目录被忽略（`so_files.size() == 1`），说明「vendor 目录下只认一层子目录」的契约。
4. **预期结果**：`_01` 到 `_04` 四个用例全过；`_04` 中 `.so1`/`.so2` 干扰文件不影响计数。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `MockMmpa` 要把目录项的 `d_type` 改成 `DT_UNKNOWN`？

**答案**：某些文件系统（如某些 XFS 配置或重定向的 FUSE）在 `readdir` 时不返回目录类型，`d_type` 为 `DT_UNKNOWN`。被测代码若依赖 `d_type == DT_DIR` 判断目录，在这种环境下会出错。Mock 强制制造该场景，验证 `ScanOppLibSubDirs`（用 `d_type == DT_DIR`）之外仍有 `mmIsDir` 兜底路径。这提示真实代码在可移植性上的敏感点。

**练习 2**：`CreateSoFile` 用 `truncate` 造大文件，为什么磁盘没有被写满 800MB？

**答案**：`truncate` 到大尺寸创建的是**稀疏文件**：文件逻辑大小为 800MB，但未写入的块不占磁盘空间，`stat` 的 `st_size` 报告逻辑大小。`ValidateSo` 恰好只看 `st_size`，所以稀疏文件足以触发大小校验分支，测试成本几乎为零。

**练习 3**：`test_plugin_manager_load`（L200–L232）中为什么把 `funcChkList` 同时填入真实存在的 `libcce.so` 与不存在的 `invalid_func` 两个名字？

**答案**：`func_check_list` 是「与」语义——所有符号都必须存在才放行。混入 `invalid_func` 保证校验必然失败，从而在真实 so（可被 dlopen）上测到「dlopen 成功但符号校验失败 → dlclose → continue」这条路径，而无需构造真正缺符号的 so 文件。

## 5. 综合实践

**任务：写出「三阶段」函数清单与故障传播表，并为优先级路径做一个纸上推演。**

结合本讲实践任务（梳理三个阶段的关键函数、入口函数名与返回值处理逻辑），完成以下三步：

1. **函数清单**：不看讲义，在白纸上默写三阶段入口函数，并用一句话写清各自的返回值语义：
   - 阶段一（搜索）：`GetOppPath`（恒 SUCCESS，环境变量优先、model path 兜底）、`GetOppPluginVendors`（格式错则 FAILED）、`GetOppPluginPathOld/New`（恒 SUCCESS，内部降级）、`GetOpsProtoPath`（断言式检查 opp path，成功 SUCCESS）。
   - 阶段二（加载）：`LoadSo/LoadSoWithFlags`（路径串通道）、`Load/LoadWithFlags`（目录通道）、`ValidateSo`（超限 FAILED）。函数级几乎恒 SUCCESS，逐 so 失败以日志表达。
   - 阶段三（符号）：`mmDlsym` 循环 + 失败 `mmDlclose`；跨 so 路径注入靠弱符号 `SetMetadefPluginCustomOpLibPathForC`。
   然后回到源码逐条核对（[plugin_manager.cc:L671-L922](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/common/plugin/plugin_manager.cc#L671-L922)），修正错漏。
2. **故障传播表**：画一张「故障 → 单 so 后果 → 整体 Load 返回值 → 日志级别」的表格，至少覆盖：路径超长、realpath 失败、单文件超 800MB、累计超 1000MB、dlopen 失败、符号缺失、数量超 64。
3. **纸上推演**：假设 `ASCEND_OPP_PATH=/usr/local/Ascend/opp/`，该目录下有 `built-in/`、`vendors/config.ini`（内容 `load_priority=mdc,lhisi`），且 `ASCEND_CUSTOM_OPP_PATH=/a:/b`（`/a/op_proto` 存在且版本合法，`/b` 不存在）。写出 `GetOpsProtoPath` 返回的完整路径串，并与用例 `test_plugin_manager_GetOpsProtoPath_08`（L495–L524）的断言风格互相印证。

预期结果：第 3 步答案应为 `/a/op_proto/:/usr/local/Ascend/opp/vendors/mdc/op_proto/:.../vendors/lhisi/op_proto/:/usr/local/Ascend/opp/built-in/op_proto/`（`/b` 被跳过；built-in 恒最后）。待本地验证。

## 6. 本讲小结

- `PluginManager` 是 metadef 的通用插件装载底座，工作分三阶段：**路径搜索**（静态函数拼冒号分隔优先级路径串）→ **dlopen 加载**（`LoadSo`/`Load` 双通道）→ **符号解析**（`func_check_list` 身份校验）。
- 路径优先级为「`ASCEND_CUSTOM_OPP_PATH`/注入路径 > `vendors/config.ini` 各厂商（按文件顺序）> `built-in`」，加载顺序即注册覆盖顺序（配合 u4-l4 的先到先得规则）。
- 版本闸门 `IsVendorVersionValid` 用 `required_opp_abi_version` 区间拦截与编译器 ABI 不兼容的厂商包；版本信息缺失时放行，`opp_latest` 独立升级包跳过校验。
- 错误处理哲学是「单 so 失败跳过、流程继续」：函数级返回值几乎恒为 SUCCESS，真实结果要看日志与 `so_list_`；仅符号缺失上升到 ERROR 并上报错误码。
- 三重限额（64 个 / 单库 800MB / 累计 1000MB）与 realpath 归一化构成对损坏或恶意算子包的防护。
- 与 u4-l5 的 opp 加载链路互补：`PluginManager` 提供通用「找-装-验」能力，算子注册表消费则由 `OppSoManager`/`AddSoToRegistry` 的两步 dlsym 协议完成，两者共享 mmpa 封装与「加载即注册」时序。

## 7. 下一步学习建议

- 下一讲 u5-l3（错误码体系与 error_manager）将展开本讲反复出现的 `GELOGE`/`REPORT_INNER_ERR_MSG`/`GE_ASSERT_TRUE` 背后的错误上报链路，建议先回看本讲 4.4.3 中 E19999 的上报点。
- 想加深 dlopen flags 理解的读者，可对照 u4-l5 的 `AddSoToRegistry`（`base/registry/opp_so_manager.cc`）比较两处 dlopen 的 flags 与去重策略差异。
- 建议继续阅读 `base/common/plugin/plugin_manager.cc` 中本讲未展开的 `GetOppSupportedOsAndCpuType`/`ScanOppLibSubDirs`（L924–L987，两层目录递归扫描 OS/CPU 架构矩阵），体会「递归深度以 layer 参数显式封顶」的防御式写法。
