# u4-l5 opp 包与 so 加载：算子实现的动态发现

## 1. 本讲目标

上一讲（u4-l4）我们搞清了「算子实现函数集如何在**单个 so 内部**注册」：`IMPL_OP` 宏利用静态对象构造期注册，把 InferShape/Tiling 等函数指针写进 so 内的 `OpImplRegistry` 单例。但那只是故事的一半——这些 so **躺在磁盘的哪个目录**？框架**什么时候、按什么顺序**把它们加载进进程？加载之后又如何把散落在各个 so 里的注册表**聚合**成一个全局可查的注册表？

学完本讲，你应该能够：

1. 说出 opp 包的目录结构（`op_proto/lib`、`op_impl/ai_core/tbe/op_tiling/lib` 等）与 so 的搜索规则（自定义算子优先于 built-in）。
2. 跟踪 `OppSoManager::LoadOppPackage()` 从「拼路径」到 `dlopen` 的完整调用链。
3. 解释 dlopen 之后注册函数被触发的两步协议：静态对象构造期注册 + `dlsym` 取 `GetRegisteredOpNum`/`GetOpImplFunctionsV2` 导出符号。
4. 掌握 `OppSoDesc`（包描述）与 `OpBinInfo`（自定义算子交付件落盘）的职责。

## 2. 前置知识

- **opp 包**：Open Programming Package，CANN 安装后存放算子交付件的目录树（通常在 `$ASCEND_HOME_PATH/opp/`）。算子交付件包括原型 so、tiling/host 实现 so、二进制等。自定义算子包安装后在 `vendors/<包名>/` 下，内置算子在 `built-in/` 下。
- **dlopen/dlsym**：Linux 动态加载接口。`dlopen(path, flags)` 把共享库装入进程（此时 so 内的**全局静态对象会执行构造函数**），`dlsym(handle, name)` 按名取符号地址。metadef 用 `mmDlopen/mmDlsym` 这层封装来兼容 Windows 等平台。
- **静态对象构造期注册**：u4-l3/u4-l4 已建立的概念——`IMPL_OP` 宏生成的静态变量在 so 被 `dlopen` 的瞬间完成注册，无需任何显式 Init 调用。
- **两步取数协议**：so 侧导出两个 C 函数——`GetRegisteredOpNum()` 返回注册条数，`GetOpImplFunctionsV2(buf, num)` 把注册表拷进调用方提供的缓冲区。这是 metadef 在 u4-l4 讲过的「跨 so 传递函数集」协议，本讲看框架侧如何消费它。
- **RTLD_NOW / RTLD_GLOBAL**：dlopen 标志。`RTLD_NOW` 立即解析全部符号（而不是首次使用时），`RTLD_GLOBAL` 把符号并入全局符号表供后续加载的 so 使用。
- **ELF**：Linux 可执行文件与共享库的格式。`OpBinInfo::Check` 通过读 ELF 头判断一个文件是不是合法的共享库。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/register/opp_so_manager.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/opp_so_manager.h) | `ge::OppSoManager` 类声明：单例，暴露 `LoadOppPackage`/`LoadOpsProtoPackage` 两个入口 |
| [base/registry/opp_so_manager.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc) | 本讲主角：搜索 opp 目录、收集 so 列表、按包聚合后交给注册表加载 |
| [inc/external/base/registry/opp_package_utils.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/registry/opp_package_utils.h) | `gert::OppSoDesc`（so 路径列表 + 包名）、`gert::OppPackageUtils::LoadAllOppPackage()` 对外门面、`OppImplVersionTag` 版本枚举 |
| [base/registry/opp_package_utils.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_package_utils.cc) | `OppSoDesc` 的 pimpl 实现（Impl 类持有 `so_paths` 与 `package_name`）与门面函数 |
| [base/registry/op_impl_space_registry_v2_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc) | `AddSoToRegistry`：真正执行 `mmDlopen`、调用取数协议、合并进 space registry |
| [base/registry/op_impl_registry_holder_manager.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc) | `OpImplRegistryHolder::GetOpImplFunctionsByHandle`：`dlsym` 找导出符号并拷出注册表；`GetOrCreateOpImplRegistryHolder`：按 so 内容去重 |
| [inc/external/register/op_bin_info.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_bin_info.h) | `ops::OpBinInfo` 声明：把内存中的自定义算子交付件写成磁盘目录 |
| [base/registry/op_bin_info.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_bin_info.cc) | `OpBinInfo` 实现：建目录、写二进制、建符号链接、ELF 校验 |
| [tests/ut/register/testcase/opp_so_manager_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/opp_so_manager_unittest.cc) | 单测：用 MmpaStub 模拟 dlopen，验证加载各目录形态的 so 列表 |

## 4. 核心概念与源码讲解

### 4.1 OppSoManager：opp 目录的搜索与 so 列表的收集

#### 4.1.1 概念说明

`OppSoManager` 回答的问题是：「磁盘上哪些 so 需要加载、按什么顺序加载」。它本身**不做 dlopen**，只负责把散落在各目录的 so 路径收集起来，按「包」聚合，然后统一交给 4.2 节的注册表加载。

opp 目录有两组 so 来源：

- **op_proto（原型 so）**：路径后缀 `/op_proto/lib/`，装的是 u4-l3 讲的 OpDef 原型注册（`OP_ADD` 宏产物）。
- **op_master / op_tiling / op_host（实现 so）**：路径后缀 `/op_impl/ai_core/tbe/op_tiling/lib/` 与 `/op_impl/ai_core/tbe/op_host/lib/`，装的是 u4-l4 讲的算子实现函数集（`IMPL_OP` 宏产物）。

搜索范围由 `PluginManager` 提供的路径函数决定（其内部读取 `ASCEND_OPP_PATH` 等环境变量，属于 u5-l2 插件管理一讲的范围），并区分两类来源：

- **自定义算子**（路径中不含 `built-in`）：直接在 `<安装路径>/lib/<os>/<cpu>/` 下找 `.so`。
- **内置算子**（路径含 `built-in`）：按是否「拆分模式」（`IsSplitOpp`）找不同的后缀——拆分模式找 `ct.so`/`rt.so`，非拆分找 `ct.so`/`rt2.0.so`。

还有一个版本维度：`OppImplVersionTag` 枚举（`kOpp`、`kOppKernel`，`kVersionEnd = 20` 预留到 20 个版本）表示 opp 包的实现协议版本，框架会对每个版本各建一套 space registry。

#### 4.1.2 核心流程

`OppPackageUtils::LoadAllOppPackage()`（外部调用入口）的处理流程：

```text
LoadAllOppPackage()                          # 对外门面，必返回 GRAPH_SUCCESS
  └─ OppSoManager::GetInstance().LoadOppPackage()   # Meyers 单例，加 mutex 锁
       for version in [kOpp, kOppKernel):           # 逐版本
         if version == kOppKernel 且非拆分模式: continue
         LoadOpsProtoSo(version, ...)               # 收集原型 so 路径
         LoadOpMasterSo(version, ...)               # 收集实现 so 路径
         for (package_name, so_list) in 收集结果:
           把 _legacy.so 后缀的 so 排到最后（stable_partition）
           LoadSoAndInitDefault(so_list, version, package_name)   # → 4.2 节
```

其中 `LoadOpsProtoSo`/`LoadOpMasterSo` 内部对每个路径：

```text
取根路径（版本 → 路径函数映射表）
按 ':' 拆分成多路径（SplitPath）
对每个路径：
  路径含 "built-in"？
  ├─ 否（自定义算子）：拼 lib/<os>/<cpu>/，mmRealPath 校验存在后
  │    GetOppSoList(path, {".so"})           # 收集全部 .so
  └─ 是（内置）：GetOppPkgPath 定位子包/主包目录
       ├─ 子包：GetOppSoList(path, {".so"})
       └─ 主包：拆分模式收集 {ct.so, rt.so}，否则 {ct.so, rt2.0.so}
```

`GetOppSoList` 把同一路径下的 so 合并到 `package_to_opp_so_desc` 这个 `vector<pair<包名, OppSoDesc>>` 中：同包名的 so 追加合并，没有则新建条目——即**以「包」为单位聚合 so 列表**。

`_legacy.so` 被移到列表末尾是一个兼容细节：新实现先注册成功后，老实现的重复注册只会打告警（u4-l4 讲过的「先到先得、不覆盖」合并规则），从而保证新实现优先。

#### 4.1.3 源码精读

`OppSoManager` 是一个极薄的单例类，只有两个公有入口和一个互斥锁：

[inc/register/opp_so_manager.h:18-36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/opp_so_manager.h#L18-L36)——声明 `LoadOppPackage()`（全量：原型 + 实现）、`LoadOpsProtoPackage()`（仅原型）和三个私有步骤函数；注意 `mutex_` 声明为 `mutable`，因为两个加载入口都是 `const` 成员函数。

[base/registry/opp_so_manager.cc:22-31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L22-L31)——常量区定义了 so 后缀与目录路径，是理解 opp 目录结构的第一手资料：`.so`、`rt2.0.so`、`ct.so`、`rt.so`、`_legacy.so` 五种后缀，`/op_proto/lib/`、`/op_impl/ai_core/tbe/op_tiling/lib/`、`/op_impl/ai_core/tbe/op_host/lib/` 三类目录。

[base/registry/opp_so_manager.cc:34-43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L34-L43)——两张「版本 → 取路径函数」映射表：`kOpp` 版本走 `GetOpsProtoPath`/`GetOpTilingForwardOrderPath`（当前安装目录），`kOppKernel` 版本走 `GetUpgradedOpsProtoPath`/`GetUpgradedOpMasterPath`（升级目录），用 map + lambda 代替 switch。

[base/registry/opp_so_manager.cc:55-99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L55-L99)——`GetOppSoList`：按后缀收集 so、把 `_legacy.so` 排到最后（L62-67 的 `stable_partition`）、把结果**按包名合并**进聚合容器（无则新建 L84-89，有则追加 L90-97）。

[base/registry/opp_so_manager.cc:140-174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L140-L174)——`LoadOppPackage` 主循环：逐版本、跳过非拆分模式下的 `kOppKernel`（L146-149）、先原型后实现地收集（L151-152）、再次把 `_legacy.so` 排尾（L157-166）、最后逐包调用 `LoadSoAndInitDefault`（L171）。

[base/registry/opp_so_manager.cc:202-232](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L202-L232)——`LoadOpsProtoSo` 的分支核心：路径不含 `built-in` 时拼 `lib/<os>/<cpu>/` 收集全部 `.so`（自定义算子，L204-214）；含 `built-in` 时按拆分与否选择 `{ct.so, rt.so}` 或 `{ct.so, rt2.0.so}`（L227-231）。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，只靠日志字符串与调用关系，手工推演出 `LoadAllOppPackage` 会扫描你机器上的哪些目录。

1. 操作步骤：
   - 阅读 [base/registry/opp_so_manager.cc:119-174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L119-L174)，抄下所有 `GELOGI` 日志关键字（如 `Start to load opp package`、`Created new opp so list for package`、`[LoadOppPackage]load so`）。
   - 在有 CANN 环境的机器上把 plog 级别调到 INFO（`export ASCEND_GLOBAL_LOG_LEVEL=1`），运行任意一个会触发算子加载的程序（如 `ge::GEInitialize` 后的图编译）。
   - 用 `grep "Created new opp so list" ~/mindspore/log/plog-*.log`（路径以实际 plog 输出目录为准）过滤日志。
2. 需要观察的现象：日志里逐条打印的包名（如 `built-in`、`vendor_xxx`）与每个包的 so 数量、so 全路径。
3. 预期结果：自定义算子包（vendors 目录）的 so 排在内置包之前被收集；`_legacy.so` 出现在各包 so 列表的末尾。
4. 本实践依赖真实昇腾环境与日志级别配置，具体输出**待本地验证**；无环境时可退化为纯源码阅读：画出 4.1.2 节流程图的完整版。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_legacy.so` 必须排在加载列表的最后？请结合 u4-l4 的合并规则回答。

**答案**：space registry 的函数合并采用「先到先得、重复告警、不覆盖」（`MERGE_FUNCTION` 宏）。若老 so 先加载，新 so 的同名函数会被拒绝。把 `_legacy.so` 排到最后，保证新实现先注册成功，老实现只触发告警，实现新旧兼容。

**练习 2**：`LoadOpsProtoPackage()` 与 `LoadOppPackage()` 的差别是什么？各自适合什么场景？

**答案**：前者只调用 `LoadOpsProtoSo`（只加载原型 so，供只需要 OpDef 信息的场景，例如算子信息查询工具）；后者额外调用 `LoadOpMasterSo`（加载 tiling/host 实现 so，供真正要执行 Tiling/推理的图编译流程使用）。

**练习 3**：`OppImplVersionTag::kVersionEnd = 20` 但当前只有 2 个有效值，这个设计解决了什么问题？

**答案**：`DefaultOpImplSpaceRegistryV2` 内部是一个按版本索引的数组（见 [base/registry/op_impl_space_registry_v2.cc:48-50](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2.cc#L48-L50)），枚举值即数组下标。预留 20 个槽位允许未来追加新的 opp 实现协议版本而无需改动数组结构；注释 `// add new version definitions here` 也表明只能在 `kVersionEnd` 之前追加，这同样是「只能尾部追加」的 ABI 契约。

### 4.2 从 dlopen 到注册触发：AddSoToRegistry 的两步协议

#### 4.2.1 概念说明

本模块承接 u4-l4 的悬案：`AddSoToRegistry` 是「so 列表 → 注册表」的桥。这里要先澄清一个容易误解的点：**metadef 侧并不存在一个名为 `Init` 的回调符号**。so 被加载后注册的触发实际由两个机制接力完成：

1. **dlopen 的副作用——静态对象构造**：`mmDlopen` 装入 so 的瞬间，so 内由 `IMPL_OP`/`OP_ADD` 宏生成的静态对象执行构造函数，把算子实现登记进 **so 自己进程空间里**的 `OpImplRegistry` 单例（这一步不需要框架调用任何 so 内函数）。
2. **dlsym 主动取数——两步协议**：框架用 `dlsym` 从 so 里找出 C 导出函数 `GetRegisteredOpNum()` 与 `GetOpImplFunctionsV2(buf, num)`，把 so 内注册表**拷贝**出来，装进 `OpImplRegistryHolder`，再逐 so 合并进 `OpImplSpaceRegistryV2`。

另外有一个重要的去重设计：**按 so 文件内容去重**。加载前先读整个 so 文件字节（`GetBinDataFromFile`），以文件内容为 key 查 `OpImplRegistryHolderManager` 的缓存——内容相同（哪怕路径不同，例如符号链接指向同一文件）的 so 不会重复 dlopen。这正是 `OpBinInfo` 建符号链接（见 4.3 节）不会导致重复注册的前提。

#### 4.2.2 核心流程

单个 so 的加载时序（本讲实践任务要求画的图）：

```text
OppSoManager::LoadSoAndInitDefault(so_list, version, pkg)
  │ 取/建该 version 的 OpImplSpaceRegistryV2（DefaultOpImplSpaceRegistryV2 单例）
  ▼
OpImplSpaceRegistryV2::AddSoToRegistry(OppSoDesc)
  │ 对 so_list 中每个 so_path：
  │   ① GetBinDataFromFile(so_path)          读文件字节，作为去重 key
  │   ② GetOrCreateOpImplRegistryHolder(so_path, so_data, create_func)
  │        ├─ 命中缓存（同内容 so 已加载）→ 直接返回已有 holder，跳过 dlopen
  │        └─ 未命中 → 执行 create_func():
  │             ③ mmDlopen(so_path, RTLD_NOW | RTLD_GLOBAL)
  │                  ★ 此刻 so 内静态对象构造 → IMPL_OP/OP_ADD 注册进 so 内单例
  │             ④ new OpImplRegistryHolder
  │             ⑤ GetOpImplFunctionsByHandle(handle, so_path):
  │                  dlsym(handle, "GetRegisteredOpNum")   → impl_num
  │                  impl_num==0 → 跳过（该 so 没有算子实现）
  │                  dlsym(handle, "GetOpImplFunctionsV2") → 取数函数
  │                  调用之，把 (op_type, funcs) 数组拷进 holder 的 map
  │                  （兼容旧版："GetOpImplFunctions" V1 路径 + ct 路径）
  │             ⑥ holder->SetHandle(handle)   保存句柄（进程期间不 dlclose）
  │   ⑦ AddRegistry(holder) → MergeTypesToImpl / MergeTypesToCtImpl
  │        合并进 space registry 的 merged_types_to_impl_（先到先得）
  └─ 自定义包兜底检查：遍历合并结果，tiling==nullptr 的算子打
     [MissOpImplementation] 告警（built-in 包与空包名不检查）
```

#### 4.2.3 源码精读

[base/registry/opp_so_manager.cc:102-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_so_manager.cc#L102-L117)——`LoadSoAndInitDefault`：为当前版本取（或新建）`OpImplSpaceRegistryV2` 并调用 `AddSoToRegistry`；`AddSoToRegistry` 失败只打告警不中断（L114-116），保证单个坏 so 不拖垮整个加载流程。

[base/registry/op_impl_space_registry_v2_impl.cc:87-100](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L87-L100)——`AddSoToRegistry` 开头：包名为 `main_exe` 走特殊分支（用 `RTLD_DEFAULT` 从已加载符号中取数，不主动 dlopen）；普通路径先读 so 文件字节（L96），再定义 `create_func` 闭包，其中 L99-100 执行 `mmDlopen(so_path, RTLD_NOW | RTLD_GLOBAL)` ——**dlopen 的返回值是后续一切 dlsym 的句柄**。

[base/registry/op_impl_space_registry_v2_impl.cc:101-119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L101-L119)——dlopen 失败的处理：拼出一段带排障指引（指向 ge 仓 wiki）的多行 ERROR 日志并 `std::cout` 打屏提示用户——算子 so 加载失败不终止进程，但要让用户看得见。

[base/registry/op_impl_space_registry_v2_impl.cc:120-135](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L120-L135)——`GE_DISMISSABLE_GUARD(close_handle, callback)` 先挂上「失败则 dlclose」的守卫；随后 `GetOpImplFunctionsByHandle(handle, so_path)` 执行 dlsym 取数；成功后 `SetHandle(handle)` 保存句柄并 `GE_DISMISS_GUARD` 撤销守卫——即**成功路径下 so 永不卸载**，注册表里的函数指针终身有效。

[base/registry/op_impl_registry_holder_manager.cc:155-161](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L155-L161)——`kImplMenuVec`：三种取数「菜单」，即两步协议的符号名表——RT_V2（`GetRegisteredOpNum` + `GetOpImplFunctionsV2`）、RT（V1 老协议 `GetOpImplFunctions`）、CT（`GetRegisteredOpCtNum` + `GetOpCtImplFunctions`）。

[base/registry/op_impl_registry_holder_manager.cc:163-204](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L163-L204)——`GetOpImplFunctionsByHandle`：先 `dlsym` 取 `GetRegisteredOpNum` 得条数（L167-175）；为 0 则 continue 尝试下一种菜单（该 so 不含此类实现，L178-180）；再 `dlsym` 取取数函数并调用，把 `(op_type, funcs)` 逐条 insert 进 holder 的 map（L181-193）；L195-201 是 V1→V2 的继承切片转换（u4-l4 讲过）。

[base/registry/op_impl_registry_holder_manager.cc:296-319](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L296-L319)——`GetOrCreateOpImplRegistryHolder`：以 **so 文件内容字符串**为 key 查缓存，命中则打日志 `so already loaded! ... no need dlopen` 直接复用（L300-307）；未命中才执行 `create_func()` 并登记（L312-318）。

[base/registry/op_impl_space_registry_v2_impl.cc:295-302](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L295-L302)——`AddRegistry`：把 holder 存入列表并触发合并；合并规则即 u4-l4 的 `MergeTypesToImpl`（「先到先得、重复告警、不覆盖」），使**同一算子的各阶段函数可以来自不同 so**（例如原型 so 提供 InferShape、host so 提供 Tiling）。

[base/registry/op_impl_space_registry_v2_impl.cc:149-165](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L149-L165)——自定义包兜底检查：非空包名且非 `built-in` 时，遍历该包合并出的算子，`tiling == nullptr` 则打 `[MissOpImplementation] op [x] has no tiling` 告警，帮助算子包作者发现漏注册。

#### 4.2.4 代码实践

**实践目标**：把「dlopen → 注册触发」的完整时序亲手跑一遍并画出时序图（本讲核心实践任务）。

1. 实践目标：验证 dlopen 失败路径与成功路径的行为差异，并产出时序图。
2. 操作步骤：
   - 打开单测 [tests/ut/register/testcase/opp_so_manager_unittest.cc:368-381](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/opp_so_manager_unittest.cc#L368-L381)（`LoadAllOppPackage_dlopen_fail`）：该用例把 `mock_handle` 设为 `nullptr`（模拟 dlopen 失败），用 `MmpaStub` 替换真实的 mmDlopen，并重定向 `std::cout` 捕获打屏输出。
   - 在仓库根目录执行 `bash tests/run_test.sh -u`（构建方式见 u1-l2），随后运行 register 单测目标，过滤该用例：`ctest -L ut -R ut_register` 或直接执行构建出的 `ut_register` 二进制加 `--gtest_filter=*LoadAllOppPackage_dlopen_fail*`。
   - 对照 4.2.2 节伪代码，把 `LoadAllOppPackage` → `LoadOppPackage` → `LoadSoAndInitDefault` → `AddSoToRegistry` →（缓存未命中）`mmDlopen` → `GetOpImplFunctionsByHandle`（dlsym 两步）→ `AddRegistry` → `MergeTypesToImpl` 这条链画成时序图，标注每步的文件与行号。
3. 需要观察的现象：单测断言捕获的 stdout 中包含 `[ERROR] Failed to load`（对应 op_impl_space_registry_v2_impl.cc L117 的打屏）；且用例正常结束——加载失败不会让进程崩溃。
4. 预期结果：`EXPECT_EQ(capture_cout.str().find("[ERROR] Failed to load") != std::string::npos, true)` 通过；时序图与本讲 4.2.2 的伪代码一致。
5. 本实践需要完整 CANN 编译环境（`ASCEND_HOME_PATH` 已 source），输出结果**待本地验证**；无环境时，可仅完成时序图绘制部分（纯源码阅读即可完成，行号已在上文列出）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AddSoToRegistry` 用「so 文件内容」而不是「so 路径」作为去重 key？

**答案**：自定义算子场景中，同一个物理 so 会被多个符号链接引用（`OpBinInfo::Generate` 会为同一个 `libcust_opapi.so` 建三个不同名字的链接，见 4.3 节）。按路径去重会重复 dlopen 同一文件；dlopen 虽然自身对同一路径有引用计数，但不同路径的符号链接会各自计入，且重复取数合并会触发大量「已被注册」告警。按内容去重最稳。

**练习 2**：加载成功的 so 为什么永不 `dlclose`？

**答案**：space registry 里保存的 `OpImplFunctionsV2` 全是**指向 so 内函数的裸指针**，一旦 dlclose，这些指针变成悬垂指针，后续调用 Tiling/InferShape 会崩溃。代码用 `GE_DISMISSABLE_GUARD`/`GE_DISMISS_GUARD` 保证只有失败路径才关闭句柄。holder 的析构函数里还显式处理了自注册 static 变量的析构顺序问题（[op_impl_registry_holder_manager.cc:320-330](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L320-L330)）。

**练习 3**：`impl_num == 0` 时直接 `continue` 而不是报错，这兼容了什么情况？

**答案**：opp 目录里有很多 so 只装**原型**（OP_ADD）或别的交付件（如二进制、tiling json），并不含 `IMPL_OP` 实现注册，其 `GetRegisteredOpNum()` 返回 0。此时跳过取数属正常情况；同理，`LoadOpsProtoPackage` 只加载原型 so，实现菜单全部为 0 也属正常。

### 4.3 OppSoDesc 与 OpBinInfo：包的描述与自定义算子的落盘

#### 4.3.1 概念说明

前两个模块讲「加载」，本模块补齐数据结构侧：

- **`gert::OppSoDesc`**：一个「包」的描述——so 路径列表 + 包名。它是 `OppSoManager`（收集方）与 `OpImplSpaceRegistryV2`（加载方）之间的传参单元。对外类只持 `unique_ptr<OppSoDescImpl>`，是 metadef 惯用的 pimpl ABI 手法（u4-l2 讲过同款）。`gert::OppPackageUtils::LoadAllOppPackage()` 是给外部（主要是 ge 仓）用的门面入口，注释明确写了优先级：**自定义算子 > 内置安装目录算子**。
- **`ops::OpBinInfo`**：反方向的工具——把**内存中**的自定义算子交付件（文件名、相对路径、二进制区间）写成一个磁盘上的 opp 目录树（`<ASCEND_WORK_PATH>/opp/<pid>_<时间戳>/vendors/`），并用符号链接把同一个 `libcust_opapi.so` 挂成 `libcust_opmaster_rt2.0.so`、`libcust_opsproto_rt2.0.so`、`liboptiling.so` 三个名字。它服务于「在进程内动态安装自定义算子包」的场景（典型消费方在 ge/acl 仓），析构时自动清理整个临时目录。

#### 4.3.2 核心流程

`OpBinInfo` 的生命周期：

```text
构造 OpBinInfo(opType, opInfo)      # opInfo: vector<(文件名, 相对路径, 起始指针, 结束指针)>
  └─ GetBasePath：优先 ASCEND_WORK_PATH 环境变量，否则 /tmp/opp/
       basePath = <根>/opp/<pid_时间戳>/vendors/
Generate(&opLibPath, targetPath)
  ├─ 逐条写二进制文件（CreateDirectory + WriteBinaryFile）
  ├─ 取系统架构（SYSTEM_PROCESSOR 或 uname）
  └─ 对 targetPath（libcust_opapi.so）创建 3 个符号链接：
       opmaster/opsproto（rt2.0 名字）+ liboptiling.so
析构 ~OpBinInfo()
  └─ DestroyCustomOpRegistry：递归删除整个临时 opp 目录树
```

写好的目录随后即可被 4.1 节的搜索逻辑发现（自定义算子路径分支），形成「内存安装 → 磁盘落地 → 搜索加载」的闭环。

#### 4.3.3 源码精读

[inc/external/base/registry/opp_package_utils.h:17-22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/registry/opp_package_utils.h#L17-L22)——`OppImplVersionTag` 枚举定义处：`kOpp`、`kOppKernel` 两个现行版本，`kVersionEnd = 20` 作数组容量。

[inc/external/base/registry/opp_package_utils.h:25-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/registry/opp_package_utils.h#L25-L47)——`OppSoDesc` 声明（pimpl，仅 `OppSoDescImplPtr impl_` 一个成员）与 `OppPackageUtils::LoadAllOppPackage()` 门面，注释「优先级：自定义算子 > 内置安装目录算子」。

[base/registry/opp_package_utils.cc:17-24](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_package_utils.cc#L17-L24)——`OppSoDescImpl` 真身：只有 `std::vector<ge::AscendString> so_paths` 和 `ge::AscendString package_name` 两个字段；注意成员用 `AscendString` 而非 `std::string`（u2-l2 讲过的跨 ABI 原则）。

[base/registry/opp_package_utils.cc:72-75](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/opp_package_utils.cc#L72-L75)——门面实现：一行转调 `OppSoManager::GetInstance().LoadOppPackage()` 后**无条件返回 `GRAPH_SUCCESS`**——因为加载失败已在内部降级为告警，不向上传播失败。

[inc/external/register/op_bin_info.h:19-27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_bin_info.h#L19-L27)——`OpInfo` 类型：`(文件名, 相对目录, 数据起始指针, 数据结束指针)` 四元组的向量，即「一份在内存里的算子交付件清单」。

[base/registry/op_bin_info.cc:110-123](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_bin_info.cc#L110-L123)——`GetBasePath`：落盘根目录选择——设了 `ASCEND_WORK_PATH` 则用之（并检查写权限），否则退到 `/tmp/opp/`；时间戳 + pid 保证并发进程互不冲突。

[base/registry/op_bin_info.cc:308-355](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_bin_info.cc#L308-L355)——`Generate`：写文件（L309-320）、取架构（L323-327）、对 `targetPath` 指向的 so 建三个符号链接（L337-352：`libcust_opmaster_rt2.0.so`、`libcust_opsproto_rt2.0.so`、`liboptiling.so`），最后通过出参 `opLibPath` 返回算子库根路径（L353）。这三个链接名正对应 4.1 节内置/自定义路径搜索的文件名——**一个物理 so 被伪装成三种交付件**。

[base/registry/op_bin_info.cc:357-392](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_bin_info.cc#L357-L392)——静态方法 `Check`：手工解析 ELF 头——校验魔数 `\x7FELF`（L370-377）、`e_type == ET_DYN`（共享库而非可执行，L378-382）、不含 `PT_INTERP` 段（没有解释器则不是可执行程序，L383-389），用于在加载前判定文件确实是合法 so。

#### 4.3.4 代码实践

**实践目标**：通过单测验证 `OpBinInfo` 的目录生成与 ELF 校验行为。

1. 操作步骤：
   - 阅读 [tests/ut/register/testcase/op_bin_info_unittest.cc:27-45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_bin_info_unittest.cc#L27-L45)（`OpBinInfoFunc`）：用例构造 `ops::OpBinInfo binInfo("add", *opInfoTest)`，调用 `Generate` 与静态 `Check`，断言返回值。
   - 执行 `bash tests/run_test.sh -u` 后运行 `ut_register` 目标并过滤 `--gtest_filter=OpBinInfoUT.*`。
   - 运行结束后检查 `/tmp/opp/` 是否已被清理（`~OpBinInfo` 递归删除目录）。
2. 需要观察的现象：`Generate` 返回 0；`Check` 对真实 so 返回 true；测试结束后临时目录消失。
3. 预期结果：`OpBinInfoUT` 全部用例通过；若中途断言失败，先确认 `/tmp` 可写（`CheckWritePermission`）。
4. 需要编译环境，**待本地验证**；无环境时的替代实践——对照 `Generate` 源码手绘它产出的目录树（含三个符号链接的指向关系），并与 4.1 节 `LoadOpsProtoSo`/`LoadOpMasterSo` 的搜索路径逐条对上。

#### 4.3.5 小练习与答案

**练习 1**：`OppPackageUtils::LoadAllOppPackage()` 为什么永远返回 `GRAPH_SUCCESS`？这样设计有什么代价与收益？

**答案**：算子 so 的可用性被设计为「尽力而为」——单个包加载失败只损失该包的算子（且有醒目打屏），不应阻断整个框架初始化。收益是健壮性（坏包不影响其他算子）；代价是调用方无法感知部分失败，只能靠日志与后续「查不到算子」的错误间接定位，所以失败路径的日志/打屏信息做得非常详细。

**练习 2**：`OpBinInfo::Generate` 为什么把同一个 so 链接成三个名字，而不是复制三份？

**答案**：链接不占额外磁盘空间且内容必然一致；更重要的是 metadef 的去重以**文件内容**为 key（4.2 节），三份拷贝若被独立 dlopen 会产生重复注册告警，而内容相同的符号链接会被缓存直接命中（`so already loaded! no need dlopen`），天然避免重复加载。

**练习 3**：`OpBinInfo::Check` 为什么要同时检查 `ET_DYN` 和 `PT_INTERP`？只查魔数不够吗？

**答案**：魔数只说明「是 ELF 文件」，但 ELF 也包括可执行文件与 `.o` 目标文件。`ET_DYN` 排除静态可执行与目标文件；但 PIE 可执行文件同样是 `ET_DYN`，所以再查 `PT_INTERP`（程序解释器段，只有可执行文件才有）把它排除，最终确保文件是可 dlopen 的共享库。

## 5. 综合实践

**任务：为「自定义算子包从安装到被调用」写出全链路追踪文档。**

假设一个算子厂商交付了自定义算子包 `MyPkg`（含 `libcust_opsproto_rt2.0.so` 与 `libcust_opmaster_rt2.0.so`），请完成：

1. **目录推演**：写出该包安装后在本机 opp 目录树中的完整路径（提示：`vendors/<包名>/op_proto/lib/<os>/<arch>/` 与 `op_impl/ai_core/tbe/op_tiling/lib/<os>/<arch>/`，os/cpu 由 `GetCurEnvPackageOsAndCpuType` 决定），标出 4.1 节搜索流程会命中的分支（自定义算子分支）。
2. **时序图**：绘制从 `gert::OppPackageUtils::LoadAllOppPackage()` 开始，到 `MyPkg` 的 Tiling 函数指针进入 `merged_types_to_impl_` 的完整时序图（参与方：OppPackageUtils、OppSoManager、OpImplSpaceRegistryV2、OpImplRegistryHolderManager、so 本身），每个箭头标注函数名、文件、行号——本文 4.2.2 节伪代码是骨架，你要补齐 `MyPkg` 特有的两个 so 各自走一遍的细节。
3. **失败注入分析**：假设 `libcust_opmaster_rt2.0.so` 依赖的 `libascendc.so` 缺失，写出用户会看到什么（打屏 `[ERROR] Failed to load ...`，源自 [op_impl_space_registry_v2_impl.cc:102-118](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L102-L118)）、后续查询该算子实现时会走到哪条兜底路径（提示：`OpImplSpaceRegistryImpl::GetOpImpl` 查不到时回落本地 `OpImplRegistry`，见 [op_impl_space_registry_v2_impl.cc:168-175](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L168-L175)）。
4. **验证**（可选，需环境）：把 2 的时序图与 plog INFO 日志逐条对照（`Start to load opp package` → `Created new opp so list for package [MyPkg]` → `Start to AddSoToRegistry so_path:...` → `Save so symbol and handle in path[...] successfully!`），确认无遗漏环节。

## 6. 本讲小结

- `OppSoManager` 负责「找 so」：按 `OppImplVersionTag` 版本 ×（op_proto / op_master）两类目录搜索，区分自定义算子（`lib/<os>/<cpu>` 下全部 `.so`）与内置算子（按拆分模式选 `ct.so`/`rt.so`/`rt2.0.so` 后缀），并按包名聚合成 `OppSoDesc`，`_legacy.so` 恒排最后。
- `AddSoToRegistry` 负责「装 so」：以 so 文件内容为 key 去重后 `mmDlopen(RTLD_NOW|RTLD_GLOBAL)`；**注册的触发没有显式 Init 回调**——dlopen 瞬间 so 内静态对象（`IMPL_OP`/`OP_ADD` 产物）自动注册，框架再用 dlsym 两步协议（`GetRegisteredOpNum` → `GetOpImplFunctionsV2`）把注册表拷出。
- 成功加载的 so 永不 dlclose（注册表存的是裸函数指针），失败只打告警与打屏、不阻断流程，自定义包还会做 `tiling` 缺失的兜底检查。
- 多个 so 的函数集按「先到先得、重复告警、不覆盖」合并进 `OpImplSpaceRegistryV2`，同一算子的不同阶段函数可来自不同 so。
- `OppSoDesc` 是 pimpl 风格的包描述（路径列表 + 包名），`OppPackageUtils::LoadAllOppPackage` 是对外门面且恒返回成功；`OpBinInfo` 则反向把内存中的自定义算子交付件落成磁盘 opp 目录（含三个指向同一 so 的符号链接），并被内容去重机制安全消化。
- 至此单元四的注册链路闭环：OpDef 定义（u4-l2）→ 原型注册（u4-l3）→ 实现注册（u4-l4）→ 磁盘发现与动态加载（本讲）。

## 7. 下一步学习建议

下一讲 u4-l6 讲解 opcheck（算子正确性校验注册），它用与 `REGISTER_OP` 同款的「静态对象构造期注册」模式登记校验函数，可与本讲的加载时序对照阅读。进入单元五后，u5-l2（插件管理）将展开本讲反复出现的 `PluginManager`——`GetOpsProtoPath`、`GetFileListWithSuffix`、`mmDlopen` 封装与 so 校验都在那里实现，是本讲天然的下半篇。建议持续阅读的源码：`base/registry/op_impl_registry_holder_manager.cc`（holder 的完整生命周期）与 `tests/ut/register/testcase/opp_so_manager_unittest.cc`（用 MmpaStub 模拟 dlopen 的测试技巧，u5-l5 会复用）。
