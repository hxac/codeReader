# Proxy 层：屏蔽昇腾硬件差异

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 HIXL Engine 为什么需要一层 proxy（代理），以及它封装的到底是哪些东西。
2. 掌握三种不同的「动态加载 + 符号解析」容错写法：dlopen/dlsym 显式加载、dlsym 失败后回退备用符号名、`__attribute__((weak))` 弱符号链接。
3. 理解 proxy 层在跨芯片代际（A2/A3/A5 等）适配中的具体作用：同一份引擎代码，在不同机器上面对不同版本、不同能力的服务库，仍能优雅降级而不是崩溃。
4. 能够追踪一个 proxy 调用点（例如 `DcmiProxy::GetDeviceInfo`），从引擎上层一路走到底层动态库。

本讲承接 u3-l1（Engine 抽象体系与工厂）。在那一讲我们看到 `EngineFactory` 会做 SoC 探测、协议判断来选择引擎；本讲往下再挖一层：工厂和 endpoint 生成器做判断所依赖的「硬件事实」，正是 proxy 层从底层库查询出来的。

## 2. 前置知识

**动态库与 dlopen/dlsym。** Linux 下程序通常在编译时链接 `.so`（加载时机固定、符号缺失会直接链接失败）。另一种方式是运行时用 `dlopen("libxxx.so", RTLD_LAZY|RTLD_NOW)` 打开库、用 `dlsym(handle, "函数名")` 查到函数地址、再通过函数指针调用。好处是：库不存在、函数不存在时程序可以自己决定怎么办（报错、降级、换别的符号名），而不是启动就崩。

**函数指针类型。** `dlsym` 返回 `void*`，需要 `reinterpret_cast` 成具体的函数指针类型（如 `int32_t (*)()`）才能调用。proxy 文件顶部那一排 `using XxxFunc = ...` 就是在描述底层 C 函数的签名。

**弱符号（weak symbol）。** C/C++ 链接器允许把符号声明为 weak：如果最终链接时找不到该符号的实现，链接不报错，该符号地址为 `nullptr`，程序运行时自行判空。这是「编译期可选依赖」的标准做法，和 dlopen 的「运行期可选依赖」互补。

**昇腾底层服务库（读者只需知道它们的存在与大致职责）。**

| 库 / 接口族 | 职责 | HIXL 用它做什么 |
|---|---|---|
| `libdcmi.so`（DCMI） | 芯片管理接口：设备 ID 换算、URMA/EID 查询、主板与超节点信息 | endpoint 生成时查拓扑事实 |
| `libdrvdsmi_host.so`（DSMI） | 驱动侧管理接口：板卡信息、slot、互联类型 | 查 slot_id、InterconType、UB 设备名 |
| `libascend_hal.so`（HAL） | 驱动最底层接口 | 主机内存注册/解注册（零拷贝前提） |
| Hcomm 接口族 | HCCL 通信通道 C 接口（endpoint/channel/mem/thread） | CS 层数据面的真正执行者 |
| `libra.so`（RA） | RoCE/RDMA 适配 | 查 notify 基地址（hccp_proxy） |

**为什么必须动态加载？** 因为这些库的版本随 CANN 版本、驱动版本、芯片代际变化：同一个能力可能换个符号名（本讲会看到真实例子）、可能在新版本才出现、也可能在某些部署形态里根本没有。HIXL 作为一个通信库，要求「在任何一台装了 CANN 的机器上都能起来，缺什么能力就降级什么能力」——静态链接做不到这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/hixl/proxy/dcmi_proxy.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc) | DCMI 代理：dlopen `libdcmi.so`，封装 ID 换算、EID 查询、设备信息查询；含**备用符号名回退**写法 |
| [src/hixl/proxy/dcmi_proxy.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.h) | `DcmiProxy` 静态类接口与 `DcmiUrmaEid`/`DcmiSpodInfo` 数据结构 |
| [src/hixl/proxy/ascend_hal_proxy.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/ascend_hal_proxy.cc) | HAL 代理：dlopen `libascend_hal.so`，封装 `halHostRegister/halHostUnregister`；**单例 Loader + scope guard** 写法 |
| [src/hixl/proxy/dsmi_proxy.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dsmi_proxy.cc) | DSMI 代理：dlopen `libdrvdsmi_host.so`；**部分符号缺失容忍**写法（可选能力） |
| [src/hixl/proxy/hcomm_proxy.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hcomm_proxy.cc) | Hcomm 代理：**弱符号**写法，CS 数据面所有通道/内存/传输操作的入口 |
| [src/hixl/proxy/hccp_proxy.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hccp_proxy.cc) | RA 代理：dlopen `libra.so`，查询 RoCE notify 基地址（本讲作为扩展阅读） |
| [src/hixl/engine/endpoint_generator/endpoint_generator.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc) | proxy 的主要「客户」之一：用 DcmiProxy/DsmiProxy 的查询结果生成端点 |
| [src/hixl/cs/transfer_pool.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc) | AscendHalProxy 的客户：主机/设备内存映射注册 |

## 4. 核心概念与源码讲解

先给一张全局图。proxy 层位于 Engine/CS 内部实现与系统库之间：

```text
┌──────────────────────────────────────────────────────┐
│  endpoint_generator / hixl_server / transfer_pool …  │  ← 引擎内部模块（只认 proxy 接口）
├──────────────────────────────────────────────────────┤
│  proxy 层                                             │
│   DcmiProxy   DsmiProxy   AscendHalProxy   HcommProxy │  ← 统一容错、加锁、日志、错误翻译
├──────────────────────────────────────────────────────┤
│  libdcmi.so  libdrvdsmi_host.so  libascend_hal.so    │  ← 系统库（版本/形态各异）
│  libra.so    Hcomm 接口（弱符号链接）                  │
└──────────────────────────────────────────────────────┘
```

三份关键源码对应三种典型封装策略，逐一精读。

### 4.1 DcmiProxy：dlopen + 备用符号名回退

#### 4.1.1 概念说明

DCMI（Device Management Interface）是芯片管理接口库 `libdcmi.so`，能回答一类「这台机器长什么样」的问题：物理 ID 与逻辑 ID 的换算、URMA 设备数量、EID 列表（UB/RDMA 通信的端点标识）、主板 ID、超节点（SPOD）信息。u3-l3 讲过，endpoint 生成需要 EID、net_instance_id 等事实——这些事实全部来自 `DcmiProxy`。

`DcmiProxy` 是纯静态类（构造函数 `= delete`，不允许实例化），因为底层库句柄是进程级全局资源，所有调用共享一份。

#### 4.1.2 核心流程

一次 `DcmiProxy::GetXxx(...)` 调用的统一流程：

```text
加锁 g_dcmi_mu
  ├─ LoadDcmiUnlocked()
  │    ├─ 已加载过？→ 直接返回缓存状态 g_dcmi_init_status
  │    ├─ TryLoadDcmiSymbols()
  │    │    ├─ dlopen("libdcmi.so", RTLD_LAZY)
  │    │    ├─ dlsym 解析 6 个函数指针（其中一个失败先试备用符号名）
  │    │    └─ 任一为空 → 记日志、dlclose、返回 -1（失败状态会被缓存）
  │    └─ InitDcmiWithRetry()
  │         └─ 调 dcmiv2_init()，失败则 sleep(1) 重试，最多 10 次
  ├─ 加载失败 或 函数指针为空 → 返回 -1
  └─ 通过函数指针调用真正的 DCMI 函数，返回其结果
```

值得注意的两个设计：

1. **失败也被缓存**（`g_dcmi_loaded = true; g_dcmi_init_status = -1`）。第一次加载失败后，后续调用不会反复重试 dlopen——避免每次查询都付出一次失败开销，也避免日志刷屏。代价是失败是「粘性」的，进程内不可恢复，只能 `UnloadDcmi()` 手动复位。
2. **初始化重试 10 秒**。`dcmiv2_init` 可能因驱动尚未就绪而失败，用 1 秒间隔轮询等它，这是对「服务启动顺序不确定」的容错。

#### 4.1.3 源码精读

符号解析与备用符号名回退——这是「跨芯片代际适配」最直接的一处证据：同一个「物理 ID 换逻辑 ID」能力，不同版本的 libdcmi 用了两个不同的符号名，proxy 先试新的、再试旧的：

```cpp
// dcmi_proxy.cc:63-68 —— 先试 dcmiv2_get_dev_id_by_chip_phy_id，
// 失败则回退到旧符号名 dcmiv2_get_dev_id_from_chip_phyid
g_dcmi_get_logicid_from_phyid =
    reinterpret_cast<DcmiGetLogicIdFromPhyIdFunc>(dlsym(g_dcmi_handle, "dcmiv2_get_dev_id_by_chip_phy_id"));
if (g_dcmi_get_logicid_from_phyid == nullptr) {
  g_dcmi_get_logicid_from_phyid =
      reinterpret_cast<DcmiGetLogicIdFromPhyIdFunc>(dlsym(g_dcmi_handle, "dcmiv2_get_dev_id_from_chip_phyid"));
}
```

对应源码：[src/hixl/proxy/dcmi_proxy.cc:63-68](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L63-L68)。这两个符号名只差介词 by/from，是新旧版本库的命名演进，proxy 用两次 dlsym 把差异吞掉，上层代码完全无感。

全量符号校验——任何一个必需符号缺失都视为整体加载失败，回滚 dlclose：

```cpp
// dcmi_proxy.cc:70-78 —— 六个符号任一为空即失败并关闭库
if (g_dcmi_init == nullptr || g_dcmi_get_urma_device_cnt == nullptr || g_dcmi_get_eid_list == nullptr ||
    g_dcmi_get_mainboard_id == nullptr || g_dcmi_get_logicid_from_phyid == nullptr ||
    g_dcmi_get_device_info == nullptr) {
  HIXL_LOGE(FAILED, "Failed to load DCMI function symbols");
  dlclose(g_dcmi_handle);
  ...
```

对应源码：[src/hixl/proxy/dcmi_proxy.cc:70-78](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L70-L78)。

初始化重试——[src/hixl/proxy/dcmi_proxy.cc:83-103](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L83-L103) 的 `InitDcmiWithRetry` 最多等 10 秒让 `dcmiv2_init` 成功，超时后 dlclose 并把失败状态缓存。

惰性加载入口——每个查询接口的第一件事都是「锁内确保已加载」，因此**调用方不需要先显式 LoadDcmi**（当然也可以显式调，例如 endpoint 生成器在 [rootinfo_builder_generator_v1.cc:81](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc#L81) 就先调了一次以便提前拿到确定的失败码）：

```cpp
// dcmi_proxy.cc:152-158 —— 加锁 → 确保加载 → 判空 → 经函数指针调用
int32_t DcmiProxy::GetLogicIdFromPhyId(uint32_t phy_id, uint32_t *logic_id) {
  std::lock_guard<std::mutex> lock(g_dcmi_mu);
  if (LoadDcmiUnlocked() != 0 || g_dcmi_get_logicid_from_phyid == nullptr) {
    return -1;
  }
  return g_dcmi_get_logicid_from_phyid(phy_id, logic_id);
}
```

对应源码：[src/hixl/proxy/dcmi_proxy.cc:152-158](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L152-L158)。`GetUrmaDeviceCnt`、`GetEidList`、`GetMainboardId`、`GetDeviceInfo` 四个接口（L160-L190）是同一个模板的复制。

#### 4.1.4 代码实践

**实践目标**：为 `DcmiProxy` 写一篇「能力-调用点」笔记，并实际验证一个调用点。

**操作步骤**（源码阅读型实践，无需 NPU 也能完成第 1-3 步）：

1. 通读 [src/hixl/proxy/dcmi_proxy.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.h)，列出它封装的 6 个底层能力（init、ID 换算、URMA 设备数、EID 列表、主板 ID、设备信息）。
2. 用下面的 grep 找出全部调用方，按「哪个引擎模块、用来做什么」归类：
   ```bash
   grep -rn "DcmiProxy::" src/ --include="*.cc" | grep -v "src/hixl/proxy"
   ```
   预期命中集中在 `src/hixl/engine/endpoint_generator/` 三个文件（endpoint_generator.cc、local_comm_res_generator_v1.cc、rootinfo_builder_generator_v1.cc）。
3. 挑选 [endpoint_generator.cc:68-78](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L68-L78) 的 `GetScaleOutNetInstanceId` 作为验证对象：它调 `DcmiProxy::GetDeviceInfo(logic_dev_id, kDcmiMainCmdChipInf, kDcmiSubCmdSpodInfo, ...)` 拿到 `DcmiSpodInfo`，再拼出 `superpod_<id>` 形式的 net_instance_id——这正是 u3-l3 讲过的 endpoint 匹配关键字段之一。
4. （可选，需真实环境）在有昇腾硬件、CANN 已加载的机器上跑一次 quickstart 样例，开 DEBUG 日志观察 DCMI 相关日志是否出现；或直接 `ldd build_out/.../libhixl.so` 确认**没有**对 libdcmi.so 的静态链接依赖——这正是动态加载的证据。

**需要观察的现象**：`ldd` 输出中不存在 `libdcmi.so`；grep 结果显示 DCMI 查询只发生在 endpoint 生成阶段（初始化路径），不在传输热路径上。

**预期结果**：得出结论——DCMI 属于「控制面/初始化期」的拓扑查询能力，proxy 把它做成了惰性、线程安全、失败可降级的开关式能力。第 4 步现象待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LoadDcmiUnlocked` 在 dlopen 失败后也要执行 `g_dcmi_loaded = true`？

**答案**：`g_dcmi_loaded` 的语义不是「加载成功」而是「已经尝试过、结论已定」。失败也置 true，后续调用直接返回缓存的 `g_dcmi_init_status = -1`，避免每次查询重复 dlopen + 刷错误日志。副作用是失败在进程内是粘性的，只有 `UnloadDcmi()` 能复位。

**练习 2**：`DcmiProxy` 的查询接口每次都要 `std::lock_guard` 加一把全局互斥锁，这在高并发下会不会成为性能瓶颈？

**答案**：不会成为实际瓶颈，因为 DCMI 查询只发生在 endpoint 生成、初始化建链等控制面路径（见上面 grep 结果），不在数据传输热路径上；而且锁内只是函数指针转发，临界区极短。这是「控制面可以全局锁、数据面绝不共享锁」的典型取舍。

**练习 3**：如果某台机器上的 libdcmi.so 既没有 `dcmiv2_get_dev_id_by_chip_phy_id` 也没有 `dcmiv2_get_dev_id_from_chip_phyid`，会发生什么？

**答案**：`TryLoadDcmiSymbols` 的全量校验（L71-78）会因该指针为空而整体失败，dlclose 库并缓存失败状态；之后所有 `DcmiProxy` 查询返回 -1，上层 endpoint 生成器把 -1 翻译成 `FAILED` 并打日志。进程不会崩溃——这正是 proxy 层存在的意义。

### 4.2 AscendHalProxy：单例 Loader + scope guard 的规范写法

#### 4.2.1 概念说明

`libascend_hal.so` 是驱动最底层的 HAL 接口。HIXL 只用到其中两个函数：`halHostRegister` / `halHostUnregister`——把一段主机内存「注册」到设备地址空间（或反向映射），得到对端 DMA 可以直接访问的设备侧地址。u2-l3 讲过的 `register_dev_addr`（主机虚拟地址在传输前被替换为设备侧映射地址）在 CS 层的落地就是靠这个 proxy 完成的。

#### 4.2.2 核心流程

```text
AscendHalProxy::HostRegister(src, size, flag, dev_id, &dst)
  ├─ 参数非空门卫（HIXL_CHECK_NOTNULL）
  ├─ EnsureLibAscendHalLoaded()
  │    ├─ 单例 LibAscendHalLoader，锁内检查 handle 已存在 → 直接返回 SUCCESS
  │    ├─ dlopen("libascend_hal.so", RTLD_NOW)
  │    ├─ dlsym 解析 halHostRegister / halHostUnregister
  │    ├─ 任一缺失 → 报错返回（scope guard 自动 dlclose）
  │    └─ 成功 → 把 handle 与两个函数指针存入单例，解除 guard
  └─ 锁内调用 ldr.host_register(...)，ret != 0 翻译成 FAILED + 详细日志
```

与 DcmiProxy 的差异点：错误类型从 `int32_t` 换成了引擎统一的 `hixl::Status`；加载成功结果**不缓存失败**（下次调用会重试 dlopen）；多了 scope guard 做异常安全回滚。

#### 4.2.3 源码精读

单例 Loader 结构——把「库句柄 + 函数指针 + 互斥锁」打包成一个 RAII 结构体，析构时自动 dlclose：

```cpp
// ascend_hal_proxy.cc:29-49 —— 局部静态单例，生命周期与进程一致
struct LibAscendHalLoader {
  void *handle = nullptr;
  HalHostRegisterFn host_register = nullptr;
  HalHostUnregisterFn host_unregister = nullptr;
  std::mutex mu;
  void Reset() { ... dlclose(handle); ... }
  ~LibAscendHalLoader() { Reset(); }
};
```

对应源码：[src/hixl/proxy/ascend_hal_proxy.cc:29-49](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/ascend_hal_proxy.cc#L29-L49)。

scope guard 保证「半成功」状态不泄漏——dlopen 成功但 dlsym 失败时，guard 会在返回前自动 dlclose：

```cpp
// ascend_hal_proxy.cc:63-81 —— RTLD_NOW 立即解析全部符号；guard 默认兜底关闭，
// 只有走到 HIXL_DISMISS_GUARD 才解除，成功路径把句柄移交单例
const int32_t dl_mode = RTLD_NOW;
void *hal_handle = dlopen(kLibAscendHalSo, dl_mode);
...
HIXL_DISMISSABLE_GUARD(handle_guard, ([hal_handle]() { (void)dlclose(hal_handle); }));
...
HIXL_DISMISS_GUARD(handle_guard);
```

对应源码：[src/hixl/proxy/ascend_hal_proxy.cc:56-83](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/ascend_hal_proxy.cc#L56-L83)。注意与 DcmiProxy 的 `RTLD_LAZY` 对比：HAL 只有 2 个符号且每次都要用，`RTLD_NOW` 更早暴露问题。

对外接口——门卫 + 确保加载 + 锁内调用 + 错误翻译四连：

```cpp
// ascend_hal_proxy.cc:93-105 —— 底层 int32_t 返回码被翻译成 hixl::Status，
// 失败日志带上 flag/logic_dev_id/size，方便定位是哪种映射、哪张卡失败
Status AscendHalProxy::HostRegister(void *src, uint64_t size, uint32_t flag, uint32_t logic_dev_id, void **dst) {
  HIXL_CHECK_NOTNULL(src);
  HIXL_CHECK_NOTNULL(dst);
  HIXL_CHK_STATUS_RET(EnsureLibAscendHalLoaded(), "[AscendHalProxy] EnsureLibAscendHalLoaded failed");
  ...
  const int32_t ret = ldr.host_register(src, size, flag, logic_dev_id, dst);
  HIXL_CHK_BOOL_RET_STATUS(ret == kDrvErrorNone, FAILED, "[AscendHalProxy] halHostRegister failed, ret=%d, ...", ...);
```

对应源码：[src/hixl/proxy/ascend_hal_proxy.cc:93-105](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/ascend_hal_proxy.cc#L93-L105)。

调用点在 CS 层 transfer_pool：`flag` 取 `kHostMemMapDevPcieTh`（主机内存映射到设备 PCIe 地址）或 `kDevSvmMapHost`（设备 SVM 区反向映射到主机），见 [src/hixl/cs/transfer_pool.cc:541](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L541) 与 [transfer_pool.cc:570](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L570)，解注册在 [transfer_pool.cc:624](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L624)。

#### 4.2.4 代码实践

**实践目标**：对比同一目录下两种 loader 组织方式的演进关系，理解「规范写法」长什么样。

**操作步骤**：

1. 并排阅读 [ascend_hal_proxy.cc:29-83](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/ascend_hal_proxy.cc#L29-L83) 与 [dcmi_proxy.cc:38-50](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L38-L50)，列一张对照表：全局裸变量 vs 单例结构体、无回滚 vs scope guard、int32_t 错误码 vs Status、RTLD_LAZY vs RTLD_NOW。
2. 再看 [dsmi_proxy.cc:38-59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dsmi_proxy.cc#L38-L59) 与 [hccp_proxy.cc:34-55](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hccp_proxy.cc#L34-L55)，确认它们与 AscendHalProxy 是同一种「单例 Loader」模式。
3. 用 `grep -rn "AscendHalProxy::" src/hixl/cs/` 确认调用面只有 transfer_pool，记录 HostRegister 的两个 flag 值各自用于哪条路径（锁页内存上行 / 设备 SVM 下行）。

**需要观察的现象**：dsmi/hccp/ascend_hal 三个 proxy 的 Loader 结构几乎逐行同构（Reset、析构 dlclose、EnsureXxxLoaded 流程），说明这是一套被复用的内部范式。

**预期结果**：写出一页「HIXL proxy loader 范式」笔记：单例 Loader + 互斥 + dlsym 校验 + scope guard + 错误翻译。这是后续给新系统库加 proxy 时的模板。

#### 4.2.5 小练习与答案

**练习 1**：`AscendHalProxy::HostRegister` 里为什么要拿两次锁概念上的「资源」——先 `EnsureLibAscendHalLoaded` 再 `lock(ldr.mu)`？

**答案**：`EnsureLibAscendHalLoaded` 内部自己加锁完成加载并把句柄存入单例；随后接口再次加同一把锁执行真正的 `host_register` 调用。两次进锁保证：加载只发生一次（幂等），且函数指针在并发调用下读取/调用是串行化的。临界区内只有一次函数指针转发，开销可忽略。

**练习 2**：如果 `halHostRegister` 返回非 0，上层看到什么？

**答案**：proxy 把驱动返回码翻译成 `hixl::Status = FAILED`，并在日志中带上 ret、flag、logic_dev_id、size 四个定位字段；transfer_pool 再经 `HIXL_CHK_STATUS` 链把错误向上传到 `RegisterMem` 的调用者（u2-l3 讲过的五层下沉的逆向路径）。

### 4.3 HcommProxy：弱符号链接 + 逐接口判空降级

#### 4.3.1 概念说明

Hcomm 是 HCCL 通信通道的 C 接口族：Endpoint（端点）创建/销毁、Mem 注册/导出/导入、Channel 创建与状态查询、通信 Thread 分配，以及最终的 `HcommReadOnThread/HcommWriteOnThread` 等**真正的数据搬运原语**。换句话说，CS 层数据面（u4 将精读）的每一步，最终都是经 `HcommProxy` 落到 Hcomm 接口上的。

它采用与 dlopen 完全不同的第三种策略：**编译期弱符号**。在源文件顶部用 `extern "C" __attribute__((weak))` 声明全部 Hcomm 函数；链接时若宿主环境（例如最终被链接进 libhixl.so 的某个部署形态）提供了实现，弱符号被解析为真实地址；若没有，符号保持 `nullptr`，proxy 运行时逐接口判空。

#### 4.3.2 核心流程

```text
编译期：__attribute__((weak)) 声明 ≈ 「可选依赖」
运行期：HcommProxy::ChannelCreate(...)
  ├─ 判空 HcommChannelCreate != nullptr ?
  │    ├─ 是 → 直接调用（无需 dlopen/dlsym，零加载开销）
  │    └─ 否 → 返回 HCCL_E_NOT_SUPPORT + 日志 "maybe unsupported"
  └─ HcclResult 转换为 HcclResult/HIXL 错误链向上传
```

判空失败返回的是 `HCCL_E_NOT_SUPPORT`（不支持），而不是崩溃或通用失败——调用方可以据此选择别的传输路径（例如回退到 RoCE/DIRECT 链路），这正是「跨芯片代际适配」的运行时体现：A3 上存在的 UB 通道能力，在不提供 Hcomm 实现的环境里表现为「不支持」，而不是「故障」。

#### 4.3.3 源码精读

弱符号声明——[src/hixl/proxy/hcomm_proxy.cc:14-71](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hcomm_proxy.cc#L14-L71) 用 `extern "C"` 块声明了约 25 个弱符号，覆盖 endpoint/mem/channel/thread/传输原语/异常回调全部能力面：

```cpp
// hcomm_proxy.cc:14-42（节选）—— weak 声明使这些符号在链接期可选
extern "C" {
__attribute__((weak)) HcommResult HcommEndpointCreate(const EndpointDesc *endpoint, EndpointHandle *endpoint_handle);
__attribute__((weak)) HcommResult HcommEndpointDestroy(EndpointHandle endpoint_handle);
...
__attribute__((weak)) HcommResult HcommChannelCreate(EndpointHandle endpoint_handle, CommEngine engine,
                                                     HcommChannelDesc *channel_descs, uint32_t channel_num,
                                                     ChannelHandle *channels);
```

逐接口判空转发——每个 proxy 方法都是同一个三段式：

```cpp
// hcomm_proxy.cc:94-98 —— 判空 → 调用 → 码型转换
HcclResult HcommProxy::EndpointCreate(const EndpointDesc *endpoint, EndpointHandle *endpoint_handle) {
  HIXL_CHK_BOOL_RET_STATUS(HcommEndpointCreate != nullptr, HCCL_E_NOT_SUPPORT,
                           "function HcommEndpointCreate is null, maybe unsupported.");
  return static_cast<HcclResult>(HcommEndpointCreate(endpoint, endpoint_handle));
}
```

对应源码：[src/hixl/proxy/hcomm_proxy.cc:94-98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hcomm_proxy.cc#L94-L98)，全文件（L74-L233）约 25 个方法均为该模式。

调用面遍布 CS 数据面——举三个代表点：

- 通道创建：[src/hixl/cs/channel.cc:53](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/channel.cc#L53) 的 `HcommProxy::ChannelCreate`（Channel 抽象的落地，u4-l3 精读）。
- 端点创建：[src/hixl/cs/endpoint.cc:97](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L97) 的 `HcommProxy::EndpointCreate`（CS Endpoint 的落地）。
- 数据搬运：[src/hixl/cs/hixl_cs_client.cc:461-463](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L461-L463) 的 `HcommProxy::ReadNbiOnThread / WriteNbiOnThread`——READ/WRITE 传输的最后一跳。

#### 4.3.4 代码实践

**实践目标**：验证弱符号在当前构建产物中的实际状态，并画出「一次 TransferSync 到 Hcomm 原语」的调用链。

**操作步骤**：

1. 若已完成 u1-l2 的构建，对产物执行：
   ```bash
   nm -D build_out/lib64/libhixl.so 2>/dev/null | grep -i "HcommEndpointCreate"
   ```
   观察该符号是 `W`（weak，未定义/可被覆盖）还是 `T`（已有实现）。
2. 源码侧跟踪调用链（纯阅读）：`Hixl::TransferSync` → `HixlImpl`（u2-l5 已走通到 HixlClient）→ handler → CS 层 `hixl_cs_client.cc:461-463` → `HcommProxy::ReadNbiOnThread/WriteNbiOnThread` → 弱符号 `HcommReadNbiOnThread`。把每一跳的文件与函数名记成列表。
3. 思考并记录：为什么 Hcomm 用弱符号而 DCMI 用 dlopen？（提示：Hcomm 是数据面热路径，每次调用判空即可、不能接受 dlopen 初始化竞态；且 Hcomm 实现随 HCCL 一起在同一链接域内提供。）

**需要观察的现象**：`nm -D` 输出中 Hcomm 符号带 `W` 标记；调用链清单上 proxy 是引擎代码触碰 Hcomm 接口的唯一位置。

**预期结果**：得到「引擎 → HcommProxy → 弱符号 → HCCL 实现」的单跳代理图。nm 结果待本地验证（依赖具体构建产物）。

#### 4.3.5 小练习与答案

**练习 1**：弱符号方案与 dlopen 方案各适合什么场景？

**答案**：弱符号适合「实现与使用者在同一链接域、调用频繁」的接口——零加载开销、一次判空即可，如 Hcomm 数据面原语；dlopen 适合「独立安装、版本多变、只在初始化期用」的库——可以回退备用符号名、运行期决定降级，如 libdcmi/libdrvdsmi。

**练习 2**：`HcommProxy::EndpointGetListenPort`（hcomm_proxy.cc:106-112）与其他方法写法略有不同，差别在哪？

**答案**：它没有用 `HIXL_CHK_BOOL_RET_STATUS` 宏，而是手写 `if (== nullptr) { HIXL_LOGI(...); return HCCL_E_NOT_SUPPORT; }`，且日志级别是 INFO 而非 ERROR——因为这个能力缺失是常见且预期内的（不是故障），用信息级日志避免误报。`BatchTransferOnThread`、异常回调两个方法同理。

**练习 3**：如果某个部署环境完全没有 Hcomm 实现，HIXL 还能工作吗？

**答案**：能。所有 Hcomm 调用返回 `HCCL_E_NOT_SUPPORT`，引擎建链/端点生成会走上层回退逻辑（例如 EngineFactory 兜底选 CommEngine、endpoint 匹配走 RoCE/DIRECT 路径，见 u3-l1/u3-l3）；进程不会崩溃。这正是 proxy 层「能力探测式降级」的设计目标。

### 4.4 扩展：DsmiProxy 的「部分符号容忍」与 HccpProxy（选读）

这两个文件不属于本讲必覆盖模块，但它们展示了 proxy 层另外两个重要的容错维度，建议快速通读：

1. **部分符号可选**：[dsmi_proxy.cc:83-88](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dsmi_proxy.cc#L83-L88) 中 `dsmi_get_board_info` 是必需符号（缺失即加载失败），而 `dsmi_get_device_info` 是可选符号——缺失只打一条 WARN，之后 `GetInterconType/GetUbDevName` 等依赖它的接口用 `IsInterconTypeSupported()`（[dsmi_proxy.cc:130-137](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dsmi_proxy.cc#L130-L137)）先探测再调用。这介于「全有全无」（DCMI）与「逐接口独立」（Hcomm）之间。
2. **头文件缺失的常量自救**：[dsmi_proxy.cc:26-29](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dsmi_proxy.cc#L26-L29) 注释明说 `kDsmiMainCmdUb = 62` 等值在 CANN 9.1.0 头文件里没有，「经真机探测确认」后以常量形式写死在 proxy 里——proxy 层吸收的不止符号差异，还有头文件版本差异。
3. **hccp_proxy**：同样是「单例 Loader + dlopen」模式，目标是 `libra.so`（RoCE/RDMA 适配），用 `RaRdevGetHandle/RaGetNotifyBaseAddr` 查询 notify 基地址，服务于 910B/910_93 类芯片的 notify 记录布局（[hccp_proxy.cc:26-28](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/hccp_proxy.cc#L26-L28) 有明确注释）。

## 5. 综合实践

**任务：产出一份《HIXL Proxy 层适配笔记》，并验证一条完整的「硬件事实查询 → 引擎决策」链路。**

1. **能力清单表**：通读 `src/hixl/proxy/` 全部 5 对 `.h/.cc`，为每个 proxy 列出：目标库、加载策略（dlopen/weak）、必需符号、可选符号、主要调用方模块。
2. **策略对比**：用一张表总结三种封装策略的取舍——
   - dlopen 全量校验 + 失败缓存（DcmiProxy）；
   - dlopen 单例 Loader + scope guard（AscendHal/Dsmi/HccpProxy）；
   - 弱符号 + 逐接口判空（HcommProxy）。
   表格维度建议：加载时机、失败语义、热路径适用性、跨版本容错手段。
3. **链路验证**（本讲实践任务的核心）：以 `DcmiProxy::GetDeviceInfo` 为例，完整跟踪一条调用链——从 [endpoint_generator.cc:68-78](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L68-L78) 的 `GetScaleOutNetInstanceId` 出发，向下到 `DcmiProxy::GetDeviceInfo`（[dcmi_proxy.cc:184-190](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/proxy/dcmi_proxy.cc#L184-L190)）再到弱耦合的 `dcmiv2_get_device_info` 函数指针；向上回答「这个查询结果（super_pod_id → net_instance_id）被 u3-l3 的哪条匹配规则消费」。把链路上每一步的入参、出参、错误处理写成时序笔记。
4. **（可选，需硬件）运行验证**：在真机上初始化一个 HIXL 实例（可复用 u1-l3 quickstart），用 `strace -f -e trace=openat ./hixl_example_quickstart ... 2>&1 | grep -E "dcmi|dsmi|ascend_hal|ra\.so"` 观察运行期实际打开了哪些 `.so`，与第 1 步清单互相印证。此步待本地验证。

## 6. 本讲小结

- proxy 层是引擎与昇腾系统库之间的统一隔离带：加锁、判空、日志、错误码翻译、版本容错全部收口在这里，引擎上层模块只认 `XxxProxy::` 静态接口。
- 三种封装策略各有适用面：dlopen + 备用符号名回退（DCMI，吸收新旧库符号改名）；单例 Loader + scope guard（HAL/DSMI/RA，规范写法）；弱符号 + 逐接口判空返回 `HCCL_E_NOT_SUPPORT`（Hcomm，数据面热路径零加载开销）。
- 容错有三个粒度：整库全有全无（DCMI）、库内部分符号可选（DSMI 的 `dsmi_get_device_info`）、逐接口独立探测（Hcomm）。
- 失败语义经过仔细设计：DCMI 失败是粘性缓存、HAL 失败可重试、Hcomm 能力缺失是「不支持」而非「故障」，支撑上层 EngineFactory/EndpointMatcher 的降级决策。
- proxy 层吸收的不止符号差异，还包括头文件常量缺失（DSMI 的 UB 命令码写真机探测值）与初始化时序不确定（DCMI init 重试 10 秒）。
- DCMI/DSMI 查询只发生在 endpoint 生成等控制面路径；AscendHal/Hcomm 则贯穿注册与数据面传输——proxy 的锁与日志策略与这条冷热分界一致。

## 7. 下一步学习建议

本讲补完了「Engine 如何感知硬件世界」的最后一层。接下来两条路：

1. **进入 u4（CS 通信服务模块）**：本讲已多次预告，`HcommProxy` 的调用方 `Endpoint`、`Channel`、`TransferPool`、`hixl_cs_client` 将在 u4-l1～u4-l3 逐一精读——你会看到 proxy 判空返回的 `HCCL_E_NOT_SUPPORT` 在 CS 层如何被翻译与传播。
2. **横向阅读建议**：若想加深对「可选依赖」模式的理解，可对照阅读 `src/hixl/common/scope_guard.h`（`HIXL_DISMISSABLE_GUARD` 的实现）与 `src/hixl/common/hixl_checker.h`（`HIXL_CHK_BOOL_RET_STATUS` 宏族），它们是本讲所有 proxy 共用的两件工具。
