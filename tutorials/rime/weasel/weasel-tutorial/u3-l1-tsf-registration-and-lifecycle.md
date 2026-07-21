# TSF IME 的注册与生命周期

## 1. 本讲目标

本讲聚焦 WeaselTSF（即编译产物 `weasel.dll`）作为 **Windows TSF（Text Services Framework）文本服务** 被系统「发现、加载、激活、停用」的完整生命周期。读完本讲，你应当能够：

1. 说清楚 `weasel.dll` 被加载进任意应用进程时，`DllMain` 做了哪些初始化，以及 COM 类工厂如何「按需」创建出 `WeaselTSF` 对象。
2. 说清楚 `DllRegisterServer` 把 Weasel 注册成一个 TSF 输入法的「三层注册」（COM 服务器、输入处理器 Profile、能力 Category）分别写了什么注册表。
3. 说清楚用户切到 Weasel 输入法时，TSF 调用 `ActivateEx` 触发的 `_Init*` 初始化链的先后顺序，哪些失败会回滚、哪些失败被刻意忽略，以及它如何与后台 `WeaselServer` 建立 IPC 连接。

本讲是 u3 单元（TSF 文本服务前端）的第一讲，只回答「WeaselTSF 这个 DLL 是怎么被系统挂载和启动的」。至于它**抓到按键之后怎么处理**、**怎么把文字写回应用文档**，留给 u3-l2（KeyEventSink）和 u3-l3（EditSession/Composition）。

## 2. 前置知识

阅读本讲前，建议先建立以下概念（不熟悉的话也能跟读，这里用一句话解释）：

- **TSF（Text Services Framework）**：Windows 提供的一套 COM 接口，让输入法、手写识别、语音等「文本服务」可以接入任意支持文本输入的应用（记事本、Word、浏览器……）。输入法不再是老的 IMM32 `.ime`，而是一个实现了一组 `ITf*` 接口的进程内 COM 服务器（in-proc server，本质是 DLL）。
- **COM 进程内服务器**：一个 DLL，导出 `DllGetClassObject` / `DllCanUnloadNow` / `DllRegisterServer` 等标准函数。系统通过 CLSID（一个 128 位 GUID）在注册表里找到这个 DLL 的路径，加载它，再让它创建出实现了某组接口的对象。
- **QueryInterface / 多接口聚合**：一个 C++ 类可以同时继承多个 COM 接口，外界调用 `QueryInterface(IID_xxx)` 问「你支持 xxx 接口吗」，对象把 `this` 转成对应接口指针返回。这样**一个对象就能扮演多个角色**。
- **Sink（事件接收器）/ Advise**：TSF 里大量使用「建议（advise）一个 sink」的模式——你把实现某接口的对象指针交给 TSF，TSF 在事件发生时（按键、文档焦点变化、写作串结束……）回调你的方法。注册 sink = `AdviseSink`，注销 = `UnadviseSink`，配对使用，返回一个 cookie 用于后续注销。
- **命名管道 IPC**：WeaselTSF（DLL，住在应用进程里）和 WeaselServer（全局唯一的后台 EXE）之间通过命名管道通信。这部分在 u2 单元已详细讲过，本讲只需要知道 `m_client` 是 IPC 客户端，`_EnsureServerConnected()` 负责确保它和后台 Server 连上了。

如果你对 u1 单元建立的全局架构（TSF 前端 ↔ Server ↔ librime）还不熟，建议先读 u1-l1。本讲依赖 u1-l2 中关于 WeaselTSF 子工程（DLL 产物、依赖关系、`.def` 导出表）的认知。

## 3. 本讲源码地图

本讲涉及的关键源码文件（均在 `WeaselTSF/` 目录与 `WeaselTSF/` 的兄弟头文件中）：

| 文件 | 作用 |
| --- | --- |
| [WeaselTSF/dllmain.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/dllmain.cpp) | DLL 入口 `DllMain`、未处理异常过滤器（写 minidump）。 |
| [WeaselTSF/Server.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp) | COM 类工厂 `CClassFactory`、`DllGetClassObject`、`DllCanUnloadNow`、`DllRegisterServer`/`DllUnregisterServer`。 |
| [WeaselTSF/Register.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp) | 三层注册的具体实现：`RegisterProfiles` / `RegisterCategories` / `RegisterServer` 及对应 Unregister。 |
| [WeaselTSF/Register.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.h) | 注册函数的声明。 |
| [WeaselTSF/WeaselTSF.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h) | `WeaselTSF` 类声明：列出了它聚合的全部 TSF 接口、`ActivateEx`/`Deactivate`、各 `_Init*`/`_Uninit*`。 |
| [WeaselTSF/WeaselTSF.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp) | `QueryInterface`、构造/析构、`Activate`/`ActivateEx`/`Deactivate`、`_EnsureServerConnected`、`_Reconnect`。 |
| [WeaselTSF/Globals.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.cpp) / [WeaselTSF/Globals.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.h) | 全局变量：模块句柄 `g_hInst`、引用计数 `g_cRefDll`、临界区 `g_cs`、CLSID/GUID 常量。 |
| [WeaselTSF/WeaselTSF.def](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.def) | DLL 导出表：声明对外暴露的 4 个函数符号。 |
| [WeaselTSF/KeyEventSink.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp) / [WeaselTSF/ThreadMgrEventSink.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ThreadMgrEventSink.cpp) / [WeaselTSF/Compartment.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp) / [WeaselTSF/LanguageBar.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp) / [WeaselTSF/DisplayAttribute.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp) | `ActivateEx` 调用的各个 `_Init*` 的具体实现，本讲会逐一对照。 |

---

## 4. 核心概念与源码讲解

### 4.1 DllMain 与类工厂

#### 4.1.1 概念说明

`weasel.dll` 是一个 **COM 进程内服务器**。这意味着：每当用户在一个应用进程里**激活**了 Weasel 输入法，Windows 的 TSF 子系统就会（在该应用进程内）按 CLSID 去注册表里找这个 DLL 的路径，加载它，然后通过它导出的标准 COM 函数创建出输入法对象。

这里要理解 COM 的两个分层：

1. **类工厂（Class Factory）**：COM 不直接 `new` 你的对象，而是先要一个「能生产对象的工厂」——实现 `IClassFactory` 的对象。系统调用 `DllGetClassObject` 拿到工厂，再调用工厂的 `CreateInstance` 生产真正的输入法对象 `WeaselTSF`。
2. **真正的文本服务对象 `WeaselTSF`**：它继承了一大堆 `ITf*` 接口，是真正干活的家伙。

而 `DllMain` 是 DLL 被加载/卸载时的回调。Weasel 在这里只做**最少且必要**的全局初始化：保存模块句柄、安装崩溃时写 minidump 的过滤器、初始化一把保护类工厂懒初始化的临界区。**真正耗时的初始化（连服务器、注册 sink）一律不放在 `DllMain` 里**——这是 COM/DLL 编程的铁律，因为 `DllMain` 持有加载器锁（loader lock），在里面做重活（如等另一个 DLL、发 IPC）极易死锁。

#### 4.1.2 核心流程

一个应用进程首次激活 Weasel 时，从 DLL 加载到对象诞生，大致经过这几步：

```
系统加载 weasel.dll
   │
   ├─ DllMain(DLL_PROCESS_ATTACH):  保存 g_hInst、装异常过滤器、初始化 g_cs
   │
系统要创建输入法对象
   │
   ├─ DllGetClassObject(CLSID, IID_IClassFactory, &pCF)
   │      └─ 双检锁懒创建全局单例 g_classFactory (CClassFactory)
   │
   ├─ pCF->CreateInstance(riid, &pObj)
   │      └─ new WeaselTSF()   ← _cRef=1，DllAddRef()
   │      └─ pObj->QueryInterface(riid)  ← AddRef，返回 ITfTextInputProcessorEx*
   │      └─ pObj->Release()   ← 抵消 new 时的引用（外部仍持有一份）
   │
系统得到 WeaselTSF 对象，随后调用它的 ActivateEx() 进入「激活态」
```

引用计数是这套机制的生命线：`DllAddRef`/`DllRelease` 操作全局计数器 `g_cRefDll`，`DllCanUnloadNow` 据此告诉系统「DLL 现在能不能卸载」。每个 `WeaselTSF` 对象构造时 `DllAddRef()`，析构时 `DllRelease()`，保证「只要还有活的输入法对象，DLL 就不会被卸载」。

#### 4.1.3 源码精读

`DllMain` 只处理两种原因，且刻意保持轻量：

[WeaselTSF/dllmain.cpp:60-73](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/dllmain.cpp#L60-L73) —— DLL 入口：保存模块句柄 `g_hInst`、安装 `_UnhandledExceptionFilter`、初始化临界区 `g_cs`；卸载时删除临界区。

其中 `_UnhandledExceptionFilter` 是 Weasel 的崩溃自保机制：输入法 DLL 跑在任意第三方应用进程里，一旦崩溃会拖垮宿主应用，所以它在崩溃时把进程和 DLL 名、时间拼成文件名，用 `MiniDumpWriteDump` 写一份 dump 到 `%TEMP%\rime.weasel`，便于事后排查：

[WeaselTSF/dllmain.cpp:11-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/dllmain.cpp#L11-L58) —— 未处理异常过滤器，生成 `应用名-DLL名-时间.进程号.dmp`。

接下来看 COM 工厂入口。`DllGetClassObject` 用**双检锁（double-checked locking）**懒初始化全局唯一的 `g_classFactory`，避免每次创建对象都加锁：

[WeaselTSF/Server.cpp:81-95](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L81-L95) —— 第一次进来若 `g_classFactory` 为空，进临界区再判一次空，然后 `BuildGlobalObjects()` 建 `CClassFactory`。

> 注意 [WeaselTSF/Globals.cpp:6](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.cpp#L6) 中 `g_cRefDll` 初始化为 `-1`，而不是 `0`。这让 [WeaselTSF/Server.cpp:97-101](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L97-L101) 的 `DllCanUnloadNow` 在「还没有任何活动对象」时也返回 `S_FALSE`（不可卸载）——因为输入法 DLL 通常希望常驻，避免反复加载/卸载的开销。

`CClassFactory::CreateInstance` 负责真正 `new` 一个 `WeaselTSF`，再走标准的 `QueryInterface → Release` 套路把对象交给调用方：

[WeaselTSF/Server.cpp:50-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L50-L65) —— `new WeaselTSF()` 后 `QueryInterface` 取接口，再 `Release()` 抵消构造时的那次引用。注释点明了关键不变量：「caller still holds ref if hr == S_OK」——即成功时 `QueryInterface` 内部的 `AddRef` 留给调用方的那份引用还在，`Release` 抵消的是 `new` 出来时 `_cRef=1` 的初始引用。

`WeaselTSF` 构造函数把所有 cookie 初始化为 `TF_INVALID_COOKIE`（这样 `Deactivate` 里 `_Uninit*` 即使没对应的 `_Init` 也能安全跳过），并 `DllAddRef()` 防止 DLL 被过早卸载：

[WeaselTSF/WeaselTSF.cpp:22-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L22-L42) —— 构造置 `_cRef=1`、各 sink cookie 置无效、`new CCandidateList(this)`、`DllAddRef()`；析构 `DllRelease()`。

最后，`WeaselTSF.def` 声明了 DLL 对外暴露的 4 个符号——这正是 COM 进程内服务器的「标准四件套」：

[WeaselTSF/WeaselTSF.def:1-7](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.def#L1-L7) —— 导出 `DllGetClassObject` / `DllCanUnloadNow` / `DllRegisterServer` / `DllUnregisterServer`，全部标记 `PRIVATE`（不进导入库，只供系统通过 `GetProcAddress` 调用）。

#### 4.1.4 代码实践

**实践目标**：搞清楚「系统要创建 WeaselTSF 对象」时，调用链上每个引用计数的增减，理解为什么对象最终不会被提前销毁。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [WeaselTSF/Server.cpp:50-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L50-L65) 的 `CClassFactory::CreateInstance`。
2. 沿着 `new WeaselTSF()` → [WeaselTSF/WeaselTSF.cpp:22-38](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L22-L38) 看 `_cRef` 设为 1。
3. 接着 `pCase->QueryInterface(riid, ppvObject)` → [WeaselTSF/WeaselTSF.cpp:44-77](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L44-L77)，里面命中分支后调用 `AddRef()`（[WeaselTSF/WeaselTSF.cpp:79-81](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L79-L81)），`_cRef` 变 2。
4. 最后 `pCase->Release()`（[WeaselTSF/WeaselTSF.cpp:83-92](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L83-L92)），`_cRef` 减回 1。

**需要观察的现象 / 预期结果**：用一张表记录 `_cRef` 的变化轨迹：

| 时刻 | 操作 | `_cRef` |
| --- | --- | --- |
| `new WeaselTSF()` | 构造函数设初值 | 1 |
| `QueryInterface` 内 `AddRef` | 命中 IID 分支 | 2 |
| `CreateInstance` 内 `Release` | 抵消构造时的引用 | 1 |

结论：调用方（TSF）最终持有一份引用（`_cRef=1`），对象存活；直到系统将来调用 `Release` 把它降到 0 时，[WeaselTSF/WeaselTSF.cpp:88-89](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L88-L89) 才 `delete this`。这是 COM 对象生命周期的标准范式。

> 本实践是纯源码追踪，不涉及运行命令；若你想实地验证，可在 `AddRef`/`Release` 临时加日志后用调试器 attach 到一个激活了 Weasel 的应用进程观察输出（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DllMain` 里只初始化临界区、保存模块句柄，而不在这里连接 `WeaselServer`？

**参考答案**：`DllMain` 执行时持有操作系统的**加载器锁（loader lock）**。如果在这里发 IPC（命名管道连接）或等待其他线程/DLL，极易造成死锁。所以 COM 进程内服务器的惯例是：`DllMain` 只做无依赖的轻量初始化，把「连服务器」「注册 sink」这类重活推迟到 `ActivateEx` 里做。

**练习 2**：`DllGetClassObject` 为什么要用「先判空、再进临界区判空」的双检锁，而不是每次直接进临界区？

**参考答案**：性能。`DllGetClassObject` 在对象创建路径上会被频繁调用，绝大多数时候 `g_classFactory` 早已建好。双检锁让热路径只在第一次真正进临界区，后续直接读指针返回，避免每次都付出临界区进出的开销；而临界区保证了首次创建的线程安全。

---

### 4.2 Register 注册文本服务

#### 4.2.1 概念说明

光有一个实现了 COM 接口的 DLL 还不够，Windows 得**知道**它的存在、把它登记成一个「输入法」才行。这就是注册。Weasel 的注册由 `DllRegisterServer`（[WeaselTSF/Server.cpp:103-109](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L103-L109)）统一驱动，但它其实做了**三件相互独立的事**，分别写到注册表的不同位置：

| 子步骤 | 函数 | 写到哪 | 解决什么问题 |
| --- | --- | --- | --- |
| ① 注册 COM 服务器 | `RegisterServer` | `HKEY_CLASSES_ROOT\CLSID\{CLSID}` 及其 `InprocServer32` 子键 | 让系统知道「这个 CLSID 由哪个 DLL 文件提供，线程模型是什么」 |
| ② 注册输入处理器 Profile | `RegisterProfiles` | TSF 的输入处理器档案库（经 `ITfInputProcessorProfileMgr`） | 让 Weasel 作为一个「中文输入法」出现在语言栏，并绑定具体语言（简体/繁体等） |
| ③ 注册能力 Category | `RegisterCategories` | TSF 的类别管理器（经 `ITfCategoryMgr`） | 声明 Weasel 具备哪些能力（键盘类、支持 UIElement、支持沉浸式……），供系统筛选 |

这三层是 TSF 输入法注册的通用范式。理解了它，你就理解了任何一个 TSF 输入法是怎么「登记在册」的。

> 注册通常由安装程序 `WeaselSetup`（见 u6-l2）调用 `regsvr32 weasel.dll` 或直接调 `DllRegisterServer` 完成，发生在安装时，而不是每次按键时。

#### 4.2.2 核心流程

`DllRegisterServer` 是总入口，三步任一失败就回滚（调 `DllUnregisterServer` 抹掉刚才写的）：

```
DllRegisterServer()
   ├─ RegisterServer()       写 HKCR\CLSID\{A3F4CDED-...}\InprocServer32 = weasel.dll 路径
   │                          ThreadingModel = "Apartment"
   ├─ RegisterProfiles()     经 ITfInputProcessorProfileMgr 注册：
   │                            简体中文(HANS, 默认启用)、繁体中文(HANT)、
   │                            香港/澳门/新加坡(禁用, 占位)
   ├─ RegisterCategories()   经 ITfCategoryMgr 把 CLSID 登记进 15 个能力类别
   │
   └─ 任一失败 → DllUnregisterServer() 回滚 → 返回 E_FAIL
```

其中 Weasel 的文本服务 CLSID 是一个写死的常量：

[WeaselTSF/Globals.cpp:10-15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.cpp#L10-L15) —— `c_clsidTextService = {A3F4CDED-B1E9-41EE-9CA6-7B4D0DE6CB0A}`。同文件还定义了 `c_guidProfile`（输入法 Profile 的 GUID）等常量。

`TEXTSERVICE_MODEL` 为 `"Apartment"`（[WeaselTSF/Globals.h:18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.h#L18)），即 COM 的**单元线程模型（Apartment）**——意味着对象的方法调用会被 COM 串行化，对象内部不必处处加锁。

#### 4.2.3 源码精读

**① COM 服务器注册**：把 CLSID 对应的 DLL 路径写进注册表。它先用 `CLSIDToStringA` 把 GUID 转成字符串形式的键名，再在 `HKCR\CLSID\{...}` 下写默认值（描述名）和 `InprocServer32` 子键（DLL 路径 + 线程模型）：

[WeaselTSF/Register.cpp:192-242](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L192-L242) —— `RegisterServer`：写 CLSID 描述、`InprocServer32` 的 DLL 全路径与 `ThreadingModel`。注意 `_M_ARM64` 分支会把路径重写指向 ARM64X 重定向器 `weasel.dll`（对应 u1-l2 提到的 `arm64x_wrapper`）。

**② Profile 注册**：用 `ITfInputProcessorProfileMgr` 把 Weasel 登记成各中文语言的输入法，并附带图标。它还读环境变量 `TEXTSERVICE_PROFILE`（`hans`/`hant`）决定默认启用简体还是繁体：

[WeaselTSF/Register.cpp:44-90](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L44-L90) —— `RegisterProfiles`。关键点：
- 默认启用简体（`hansEnable = hansEnable || (!hantEnable && !hansEnable)`，即「没指定时就启用简体」）。
- 简体/繁体调用 `FindIME` 去注册表 `SYSTEM\...\Keyboard Layouts` 里找老的 IMM32 `weasel.ime` 残留的 HKL，做兼容桥接（[WeaselTSF/Register.cpp:13-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L13-L42)）。
- 香港/澳门/新加坡三个语言以 `enable=false` 注册（占位，不启用），且 HKL 为 `NULL`。

**③ Category 注册**：把 CLSID 登记进一组能力类别，让 TSF 知道 Weasel「能干什么」：

[WeaselTSF/Register.cpp:108-131](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L108-L131) —— `RegisterCategories` 遍历 `SupportCategories0` 数组调 `RegisterCategory`。

[WeaselTSF/Register.cpp:108-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L108-L118) —— `SupportCategories0` 列出全部 15 个类别，含义包括：
- `GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT`：支持 Windows 8 应用（UWP/沉浸式）；
- `GUID_TFCAT_TIPCAP_UIELEMENTENABLED`：支持 UIElement（候选列表以系统 UIElement 方式呈现）；
- `GUID_TFCAT_TIPCAP_COMLESS`：无依赖 COM 组件；
- `GUID_TFCAT_DISPLAYATTRIBUTEPROVIDER`：提供写作串的显示属性（如下划线）。

> 卸载走对称的 `DllUnregisterServer` → `UnregisterProfiles` / `UnregisterCategories` / `UnregisterServer`（[WeaselTSF/Server.cpp:111-116](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Server.cpp#L111-L116)）。其中 `UnregisterServer` 还会额外清理 Windows 8 残留的 `Software\Microsft\CTF\TIP\{CLSID}` 键（注意源码里 `Microsft` 是历史拼写，并非笔误）。

#### 4.2.4 代码实践

**实践目标**：在注册表里「亲眼看见」Weasel 注册写下的三层痕迹，把抽象的注册函数对应到具体的注册表键。

**操作步骤**（需在装好 Weasel 的 Windows 上运行，待本地验证）：

1. 以管理员身份打开 `regedit`。
2. 定位到 `HKEY_CLASSES_ROOT\CLSID\{A3F4CDED-B1E9-41EE-9CA6-7B4D0DE6CB0A}`，查看其 `InprocServer32` 子键：默认值应指向 `weasel.dll` 的安装路径，`ThreadingModel` 应为 `Apartment`。这对应 `RegisterServer`。
3. （可选）用 PowerShell 查询 TSF profile：
   ```powershell
   # 列出所有注册的 TSF 输入处理器，找 Weasel 的 CLSID
   Get-WinUserLanguageList
   ```
   或在 `控制面板 → 语言` 里确认「小狼毫」出现在中文输入法列表中。这对应 `RegisterProfiles`。

**需要观察的现象 / 预期结果**：

- `InprocServer32` 的 DLL 路径就是磁盘上真实的 `weasel.dll` 位置。
- 如果你是从源码构建并 `output\install.bat` 安装的，路径应在 `output\` 目录下。

**预期结果**：你能把 `regedit` 里看到的三个键（CLSID 描述、InprocServer32、Profile）分别对应回本节讲的 `RegisterServer` 和 `RegisterProfiles` 两段源码。Category 类别存在 TSF 内部数据库中，普通用户看不到，但可在 [WeaselTSF/Register.cpp:108-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L108-L118) 的数组里逐条核对。

> 若当前没有 Windows 环境或未安装 Weasel，本实践可退化为「源码阅读型」：对照 [WeaselTSF/Register.cpp:192-242](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L192-L242) 把每条 `RegSetValueExA` 调用翻译成「写到了哪个键的哪个值」。

#### 4.2.5 小练习与答案

**练习 1**：`RegisterProfiles` 里，为什么香港、澳门、新加坡三种语言用 `enable=false`（即 `register_profile(..., NULL, false)`）注册？

**参考答案**：Weasel 实际只对简体（HANS）和繁体（HANT）做输入处理。但为了在 TSF 的 Profile 体系里「占位」、避免与系统其它输入法冲突或保证卸载时能干净清掉这些语言的档案，它仍然把这几个语言以禁用状态登记进来。`UnregisterProfiles`（[WeaselTSF/Register.cpp:92-106](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L92-L106)）会无差别地反注册这 5 种语言，保持注册/反注册对称。

**练习 2**：`DllRegisterServer` 为什么在三步中任一步失败后要调 `DllUnregisterServer`？

**参考答案**：注册是「半成品」状态会留下脏数据——比如 COM 服务器登记了，但 Profile 没登记，系统就会找到一个「登记了却无法作为输入法出现」的幽灵 CLSID。失败时整体回滚，保证要么「全注册成功」、要么「一点不留」，状态干净。

---

### 4.3 ActivateEx 初始化链

#### 4.3.1 概念说明

注册只是让系统「认识」Weasel；真正「开始工作」发生在用户把输入法切到 Weasel 的那一刻——TSF 会调用对象上的 `ActivateEx`（老接口 `Activate` 也保留）。这是 WeaselTSF 生命周期的**核心时刻**：它要把所有需要的「事件接收器（sink）」挂到 TSF 上、初始化候选窗口、并和后台 `WeaselServer` 建立 IPC 连接。

要理解 `ActivateEx`，先记住 `WeaselTSF` 聚合了一大堆 TSF 接口——**一个对象同时扮演多个角色**：

[WeaselTSF/WeaselTSF.h:11-20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L11-L20) —— `WeaselTSF` 继承的 10 个接口，包括 `ITfTextInputProcessorEx`（文本服务本体）、`ITfKeyEventSink`（按键）、`ITfThreadMgrEventSink`（文档焦点）、`ITfTextEditSink`/`ITfTextLayoutSink`（文档编辑/布局）、`ITfCompositionSink`（写作串）、`ITfThreadFocusSink`（线程焦点）、`ITfEditSession`（文档写入会话）、`ITfDisplayAttributeProvider`（写作串显示属性）等。

`QueryInterface` 把这些接口一一对外暴露，外界要哪个就 `static_cast` 到哪个：

[WeaselTSF/WeaselTSF.cpp:44-77](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L44-L77) —— `QueryInterface`：对每个支持的 IID 把 `this` 转成对应接口指针，`AddRef` 后返回；都不匹配返回 `E_NOINTERFACE`。

`ActivateEx` 的工作，本质上就是**把「自己」作为 sink 注册到 TSF 的各个管理器上**——因为 `this` 同时是按键 sink、编辑 sink、焦点 sink，所以注册时只要传 `(ITfXxxSink*)this` 即可。

#### 4.3.2 核心流程

`ActivateEx` 是一条「串行初始化链」，每一步 `_Init*` 都可能失败。失败处理分两类：**受检**（失败则 `goto ExitError` 整体回滚）与**非受检**（失败也继续，记日志或后续重试）。这是本讲最需要吃透的设计点。

```
ActivateEx(pThreadMgr, tfClientId, dwFlags)
   ├─ 保存 _pThreadMgr / _tfClientId / _activateFlags
   ├─ _InitThreadMgrEventSink()        ★受检：注册文档焦点 sink
   ├─ 若当前有焦点文档：_InitTextEditSink(pDocMgrFocus)   非受检：注册编辑/布局 sink
   ├─ _InitKeyEventSink()              ★受检：注册按键 sink（抓键的核心）
   ├─ _InitDisplayAttributeGuidAtom()  非受检：登记显示属性 GUID（部分 App 不支持）
   ├─ _InitPreservedKey()              ★受检（当前恒返回 TRUE）：保留键
   ├─ _InitLanguageBar()               ★受检：创建语言栏按钮
   ├─ 若键盘未开：_SetKeyboardOpen(TRUE)
   ├─ _InitCompartment()               ★受检：注册开/关与中英状态变化 sink
   ├─ _InitThreadFocusSink()           ★受检：注册线程焦点 sink
   ├─ _EnsureServerConnected()         非受检：连后台 WeaselServer（连不上后续按键会重试）
   └─ return S_OK
ExitError:
   └─ Deactivate()   回滚（_Uninit* 全部幂等安全）→ return E_FAIL
```

**为什么有些步骤非受检？** 这是工程上的务实取舍：

- `_InitTextEditSink` 依赖「当前有没有焦点文档」——刚激活时可能还没有，而且 `OnSetFocus` 回调（[WeaselTSF/ThreadMgrEventSink.cpp:12-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ThreadMgrEventSink.cpp#L12-L28)）稍后会再次调用它补救，所以这里失败不该中止激活。
- `_InitDisplayAttributeGuidAtom` 的源码注释直接写明：「some app might init failed because it not provide DisplayAttributeInfo, like some opengl stuff」——某些应用（如 OpenGL 程序）不提供显示属性信息，若因此中止激活，Weasel 在这些应用里就完全用不了。
- `_EnsureServerConnected` 连的是后台进程，Server 可能还没起来；这里不中止，留给每次按键时 `_ProcessKeyEvent` → `_EnsureServerConnected`（[WeaselTSF/KeyEventSink.cpp:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L20-L23)）去重试，体验更平滑。

#### 4.3.3 源码精读

`ActivateEx` 全貌，注意每一步后面是否跟着 `goto ExitError`：

[WeaselTSF/WeaselTSF.cpp:123-170](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L123-L170) —— `ActivateEx` 初始化链与 `ExitError` 回滚分支。

把链上每个 `_Init*` 的实现逐一对照（按调用顺序）：

1. **`_InitThreadMgrEventSink`** —— 通过 `_pThreadMgr` 的 `ITfSource` 把自己 `AdviseSink` 成 `ITfThreadMgrEventSink`，拿到 cookie。失败返回 FALSE → `goto ExitError`：
   [WeaselTSF/ThreadMgrEventSink.cpp:38-51](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ThreadMgrEventSink.cpp#L38-L51)。

2. **`_InitTextEditSink`** —— 给焦点文档注册 `ITfTextEditSink`/`ITfTextLayoutSink`。注意它先「清除上一次的 sink」，再对新文档建新 sink；传入 `NULL` 时只清除不新建（这点对 `Deactivate` 很关键）：
   [WeaselTSF/TextEditSink.cpp:73-91](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L73-L91)。

3. **`_InitKeyEventSink`** —— 从 `_pThreadMgr` 取 `ITfKeystrokeMgr`，`AdviseKeyEventSink` 把自己注册成按键 sink（传 `TRUE` 表示「现在就把按键交给我」）。失败 → `goto ExitError`。这是抓键的入口，下一讲 u3-l2 会展开：
   [WeaselTSF/KeyEventSink.cpp:156-167](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L156-L167)。

4. **`_InitDisplayAttributeGuidAtom`** —— 经 `ITfCategoryMgr` 把显示属性 GUID 注册成一个 guid atom。非受检（注释解释了原因）：
   [WeaselTSF/DisplayAttribute.cpp:55-70](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/DisplayAttribute.cpp#L55-L70)。

5. **`_InitPreservedKey`** —— 当前实现直接 `return TRUE`，真正的保留键注册代码被 `#if 0` 注释掉了（保留扩展位）：
   [WeaselTSF/KeyEventSink.cpp:178-199](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L178-L199)。

6. **`_InitLanguageBar`** —— 取 `ITfLangBarItemMgr`，新建 `CLangBarItemButton`（输入法状态按钮）并 `AddItem`，再 `Show(TRUE)`。失败 → `goto ExitError`：
   [WeaselTSF/LanguageBar.cpp:367-387](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/LanguageBar.cpp#L367-L387)。

7. **`_InitCompartment`** —— 创建两个 `CCompartmentEventSink`，分别监听 `GUID_COMPARTMENT_KEYBOARD_OPENCLOSE`（键盘开/关）和 `GUID_COMPARTMENT_KEYBOARD_INPUTMODE_CONVERSION`（中/英等转换模式）的变化。失败 → `goto ExitError`：
   [WeaselTSF/Compartment.cpp:215-231](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Compartment.cpp#L215-L231)。

8. **`_InitThreadFocusSink`** —— 把自己 `AdviseSink` 成 `ITfThreadFocusSink`，用于在应用窗口获得/失去焦点时做相应处理（如 `OnKillThreadFocus` 时 `_AbortComposition`）。失败 → `goto ExitError`：
   [WeaselTSF/WeaselTSF.cpp:190-199](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L190-L199)。

9. **`_EnsureServerConnected`** —— 非受检的最后一关：若 `m_client.Echo()` 探测不到后台 Server，则重试若干次；连续 6 次失败后，会启动一个后台线程去执行 `start_service.bat` 拉起 `WeaselServer.exe`，再重连：
   [WeaselTSF/WeaselTSF.cpp:238-281](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L238-L281)。

**回滚的幂等性**：`ExitError` 调用的 `Deactivate` 会按相反顺序 `_Uninit*` 一切。关键是这些 `_Uninit*` 都是**幂等安全**的——它们先检查 cookie 是否有效、指针是否为空，再决定是否 `UnadviseSink`。因此即便某个 `_Init*` 在链中途失败、后面的 `_Init*` 还没执行，`Deactivate` 也能安全地把已注册的部分干净拆掉：

[WeaselTSF/WeaselTSF.cpp:98-121](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L98-L121) —— `Deactivate`：`EndSession` → `_InitTextEditSink(NULL)`（借「传 NULL 只清除」的语义拆掉编辑 sink）→ 各 `_Uninit*` → 清空 `_pThreadMgr`/`_tfClientId` → `_cand->DestroyAll()`。

> 顺带一个阅读彩蛋：`Deactivate` 里 `_UninitThreadMgrEventSink()` 被调用了两次（[WeaselTSF/WeaselTSF.cpp:103](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L103) 与 [WeaselTSF/WeaselTSF.cpp:112](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L112)）。第二次是空操作，因为 [WeaselTSF/ThreadMgrEventSink.cpp:53-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ThreadMgrEventSink.cpp#L53-L62) 的实现会检查 `cookie == TF_INVALID_COOKIE` 提前返回——这正是「幂等」设计带来的容错。

#### 4.3.4 代码实践

**实践目标**（即本讲指定的实践任务）：在 `ActivateEx` 中标注每个 `_Init*` 调用的作用，并说明若某个受检 `_Init` 失败会走到 `ExitError` 的哪一步、产生什么后果。

**操作步骤**（源码阅读 + 填表）：

1. 打开 [WeaselTSF/WeaselTSF.cpp:123-170](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L123-L170)。
2. 为下面 9 个步骤填一张「三列表」：①它注册了什么 sink/做了什么；②是否受检（失败是否 `goto ExitError`）；③失败时的后果。

**参考答案表**：

| # | 调用 | 作用 | 受检？ | 失败后果 |
| --- | --- | --- | --- | --- |
| 1 | `_InitThreadMgrEventSink` | 注册文档焦点变化 sink | 是 | `goto ExitError` → `Deactivate` 回滚 → `ActivateEx` 返回 `E_FAIL`，**此次激活失败**，输入法本次不可用 |
| 2 | `_InitTextEditSink` | 给焦点文档注册编辑/布局 sink | 否 | 不中止；稍后 `OnSetFocus` 会再次尝试注册 |
| 3 | `_InitKeyEventSink` | 注册按键 sink（抓键核心） | 是 | 激活失败——抓不到任何键，输入法形同虚设，所以必须中止 |
| 4 | `_InitDisplayAttributeGuidAtom` | 登记显示属性 GUID | 否 | 不中止；写作串可能没有下划线等显示属性，但能输入 |
| 5 | `_InitPreservedKey` | 保留键（当前恒 TRUE） | 是（但实际不触发） | 当前代码恒返回 TRUE，不会失败 |
| 6 | `_InitLanguageBar` | 创建语言栏按钮 | 是 | 激活失败——没有语言栏按钮会很不便，故中止 |
| 7 | `_SetKeyboardOpen(TRUE)` | 确保键盘处于开启 | — | 无返回值检查，只是设置状态 |
| 8 | `_InitCompartment` | 注册开/关、中英转换监听 | 是 | 激活失败——无法响应状态切换，故中止 |
| 9 | `_InitThreadFocusSink` | 注册线程焦点 sink | 是 | 激活失败——无法在失焦时清写作串，故中止 |
| 10 | `_EnsureServerConnected` | 连后台 Server | 否 | 不中止；每次按键时 `_ProcessKeyEvent` 会再次 `_EnsureServerConnected` 重试 |

3. **追踪失败回滚路径**：假设第 8 步 `_InitCompartment` 失败，从 [WeaselTSF/WeaselTSF.cpp:158-161](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L158-L161) `goto ExitError` → [WeaselTSF/WeaselTSF.cpp:167-169](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L167-L169) 调 `Deactivate()` → 返回 `E_FAIL`。`Deactivate` 会把步骤 1、3、6 已经注册的 sink 全部 `Unadvise` 掉（步骤 8、9 因未执行，其 `_Uninit*` 因 cookie 无效而安全跳过），对象回到干净状态。

**需要观察的现象 / 预期结果**：

- 你应当能说清楚：「受检」步骤都是「缺了它输入法就不完整/不安全」的环节（抓键、焦点、状态、语言栏）；「非受检」步骤都是「缺了也能勉强工作、或后续会补救」的环节（显示属性、文本编辑 sink、Server 连接）。
- 这种分类体现了**防御式设计**：宁可激活失败也不要一个「半残」的输入法；但同时对「环境相关、可能暂时失败」的环节留出容忍空间。

> 本实践是源码阅读型，不运行命令。若想动态验证某个 `_Init` 失败的行为（待本地验证），可在调试器里于某 `_Init*` 强制返回 FALSE，观察 `ActivateEx` 是否返回 `E_FAIL` 且已注册的 sink 被正确反注册。

#### 4.3.5 小练习与答案

**练习 1**：`Activate`（老接口）和 `ActivateEx`（新接口）是什么关系？Weasel 实际用的是哪个？

**参考答案**：`Activate` 是 `ITfTextInputProcessor` 的方法，`ActivateEx` 是其扩展接口 `ITfTextInputProcessorEx` 的方法，多了一个 `dwFlags` 参数（可携带 `TF_TMF_IMMERSIVEMODE` 等标志）。Weasel 实现了 `Activate` 直接转调 `ActivateEx(pThreadMgr, tfClientId, 0U)`（[WeaselTSF/WeaselTSF.cpp:94-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L94-L96)），所以无论系统调哪个，最终都走 `ActivateEx`。`dwFlags` 被存进 `_activateFlags`，供 `isImmersive()`（[WeaselTSF/WeaselTSF.h:194-196](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.h#L194-L196)）判断是否运行在 UWP/沉浸式环境下。

**练习 2**：`_EnsureServerConnected` 为什么不做成受检步骤（失败就 `goto ExitError`）？

**参考答案**：后台 `WeaselServer.exe` 可能尚未启动、正在重启或临时无响应。若把它设为受检，激活会直接失败、用户在这一刻完全无法输入。当前设计让激活照常成功，把「连不上 Server」的恢复推迟到每次按键时——`_ProcessKeyEvent` 开头会再调 `_EnsureServerConnected`（[WeaselTSF/KeyEventSink.cpp:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L20-L23)），连不上就 `*pfEaten = FALSE`（放行按键、不拦截），等 Server 恢复后自然恢复输入，体验平滑得多。

**练习 3**：`Deactivate` 里 `_InitTextEditSink(com_ptr<ITfDocumentMgr>())`（传一个空智能指针）为什么能起到「清理」作用？

**参考答案**：`_InitTextEditSink` 内部开头有一段「先清除上一次 sink」的逻辑（[WeaselTSF/TextEditSink.cpp:77-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L77-L86)），随后判断 `if (pDocMgr == NULL) return TRUE;`（[WeaselTSF/TextEditSink.cpp:87-88](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/TextEditSink.cpp#L87-L88)）。所以传 NULL 正好触发「清除旧 sink 后立刻返回」，是一个巧妙的复用——同一个函数既是「注册新 sink」又是「注销旧 sink」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**生命周期全链路追踪**任务。

**任务背景**：假设你是一名新加入 Weasel 维护团队的工程师，被要求写一份「WeaselTSF 从安装到首次按键可用」的内部技术备忘。你需要把本讲的三层内容编织成一条完整叙事。

**要求产出**：

1. **绘制一张完整的「时间轴图」**，横轴是时间，纵轴标注发生的事件，至少包含以下里程碑，并为每个里程碑标注对应源码位置：
   - 安装时：`regsvr32`/安装器调用 → `DllRegisterServer` → `RegisterServer` + `RegisterProfiles` + `RegisterCategories`。
   - 用户首次在某应用切到 Weasel：系统加载 `weasel.dll` → `DllMain(DLL_PROCESS_ATTACH)`。
   - 系统创建对象：`DllGetClassObject` → `CClassFactory::CreateInstance` → `new WeaselTSF()`。
   - 系统激活：`ActivateEx` → 9 个 `_Init*`（标注哪些受检）→ `_EnsureServerConnected`。
   - （留接口）用户按键：`OnTestKeyDown` → `_ProcessKeyEvent`（这一段属 u3-l2，本讲只需画个占位框并注明「见下一讲」）。

2. **写一段「失败模式分析」**，回答：如果 `WeaselServer.exe` 没有在运行，用户切到 Weasel 并按键，会发生什么？请用本讲学到的知识解释：
   - 激活阶段（`ActivateEx`）不会失败，因为 `_EnsureServerConnected` 非受检；
   - 但按键阶段 `_ProcessKeyEvent` 调 `_EnsureServerConnected` 探测失败时会把 `*pfEaten` 置 `FALSE`（放行），并在连续 6 次失败后尝试用 `start_service.bat` 拉起 Server（[WeaselTSF/WeaselTSF.cpp:238-281](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.cpp#L238-L281)）；
   - 期间用户输入的字母会「直通」上屏（因为没被吃键），看起来像英文直出。

3. **检查清单**：列出 3 个你在阅读源码过程中产生的疑问，标注哪些可以在后续讲义（u3-l2/u3-l3/u3-l4）找到答案、哪些需要自行查阅 TSF 官方文档。

> 本实践重在「把零散知识结构化」，建议手画或用任意画图工具完成时间轴。不要求运行代码。

## 6. 本讲小结

- `weasel.dll` 是一个 **COM 进程内服务器**，导出标准四件套（`DllGetClassObject`/`DllCanUnloadNow`/`DllRegisterServer`/`DllUnregisterServer`）；`DllMain` 只做最轻量的全局初始化（模块句柄、崩溃 minidump 过滤器、临界区），重活全部推迟。
- 对象创建走 `DllGetClassObject` →（双检锁懒建的）`CClassFactory::CreateInstance` → `new WeaselTSF()` → `QueryInterface`；引用计数 `_cRef` 与全局 `g_cRefDll` 共同决定对象与 DLL 的存活。
- 注册分**三层**：`RegisterServer`（COM CLSID + `InprocServer32` + `ThreadingModel=Apartment`）、`RegisterProfiles`（TSF 输入处理器档案，简体默认启用、繁体可选、港澳新禁用占位）、`RegisterCategories`（15 个能力类别）；任一失败整体回滚。
- `WeaselTSF` 聚合 10 个 TSF 接口，靠 `QueryInterface` 对外暴露；`ActivateEx` 是一条串行 `_Init*` 初始化链，把「自己」作为各类 sink 注册到 TSF。
- 初始化分**受检**（失败 `goto ExitError` → `Deactivate` 回滚 → 返回 `E_FAIL`）与**非受检**（失败继续）两类；`_InitTextEditSink`、`_InitDisplayAttributeGuidAtom`、`_EnsureServerConnected` 是非受检的，体现「对环境/时序相关失败留容忍」的务实设计。
- `_Uninit*` 全部幂等安全，保证无论 `ActivateEx` 在链上何处失败，`Deactivate` 都能把已挂载的 sink 干净拆掉。

## 7. 下一步学习建议

本讲只回答了「WeaselTSF 怎么被挂载和激活」。要完整理解前端，建议按以下顺序继续：

1. **u3-l2 按键事件捕获 KeyEventSink**：深入 `KeyEventSink.cpp` 的 `OnTestKeyDown`/`OnKeyDown` 去重策略、`_ProcessKeyEvent` 如何把 Windows 按键转成 `weasel::KeyEvent` 并通过 IPC 发给 Server。这是本讲 `_InitKeyEventSink` 注册的那个 sink 的真正用武之地。
2. **u3-l3 编辑会话与上屏**：讲 `EditSession`/`Composition` 如何把 preedit 写回应用文档、如何 commit 最终文字——对应本讲 `_InitTextEditSink` 注册的编辑 sink。
3. **u3-l4 候选列表、语言栏与显示属性**：展开 `CCandidateList`、`LanguageBar`、`DisplayAttribute`、`Compartment`——即本讲 `_InitLanguageBar`/`_InitCompartment`/`_InitDisplayAttributeGuidAtom` 注册的几个模块的细节。
4. **若想了解「连服务器」更底层**：回到 u2 单元（IPC 骨架），特别是 u2-l3 的 `Client::Connect` → `StartSession`，理解本讲 `_EnsureServerConnected`/`_Reconnect` 背后的 IPC 机制。
5. **若想了解「安装时如何调用注册」**：跳到 u6-l2（WeaselSetup 与 IME 注册），看安装程序怎么触发本讲的 `DllRegisterServer`。
