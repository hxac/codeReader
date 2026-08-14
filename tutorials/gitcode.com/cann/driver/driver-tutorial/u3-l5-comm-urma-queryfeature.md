# 通信层 comm/urma 适配与 queryfeature 兼容查询

## 1. 本讲目标

本讲是单元 3（HAL 层与主机-设备通信）的收尾篇。前面几讲我们建立了两条通信主干：u3-l2 讲了 **HDC**——主机（Host）与设备（Device）之间收发消息的底座；u3-l3、u3-l4 讲了 **PBL**——UDA 翻译设备号、URD 分发用户请求、commlib 提供公共设施。本讲再补上两块拼图：

1. **设备到设备（Device-to-Device）的内存共享通信**：当多张 NPU 之间、或主机与设备之间需要共享一大块内存时，驱动用一套叫 **URMA** 的外部 RDMA 框架来完成。`comm/ascend_urma_adapt` 就是把昇腾 HAL 的设备模型「适配」到 URMA 框架上的薄胶水层。
2. **软件特性查询（queryfeature）**：driver 要同时支持 ascend910B、ascend950 等多种芯片，不同芯片支持的特性不同。`pbl/queryfeature` 提供一个统一的 `halSupportFeature(devId, type)` 接口，让上层模块在运行期「先问一句：当前芯片支持这个特性吗？」再决定走哪条代码路径。

学完本讲，你应当能够：

- 说清 `ascend_urma_adapt` 适配层为什么存在、它把 HAL 的什么概念映射成 URMA 的什么概念；
- 跟踪 URMA 从库自动初始化（`urma_init`）→ 设备发现（`ascend_urma_get_device`）→ context/token/segment 资源管理的完整链路；
- 理解 `query_feature.c` 的「函数指针表 + 枚举下标」表驱动设计，并能动手新增一个特性判断；
- 区分「编译期宏适配」与「运行期特性查询」两种多芯片兼容手段，以及它们为何要配合使用。

## 2. 前置知识

本讲默认你已经学完 u1-l1（三层架构）、u1-l5（公共头文件）、u3-l1（HAL 与 `halGetDeviceInfo`）、u3-l2（HDC 通信）。下面补充几个本讲会用到的术语。

- **URMA**：一套统一的 RDMA 通信框架，由外部头文件 `urma_api.h` / `urma_types.h` 提供（[ascend_urma_init.c:10](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_init.c#L10)）。它的核心抽象有：device（设备）、context（上下文）、EID（Endpoint ID，端点地址，类似 verbs 里的 GID/QP 号）、token（访问令牌）、segment（可被远端访问的内存段）、jetty/JFC/JFR（收发与完成计数原语）。本讲只关心前五个，jetty/JFC 出现在最底层的 raw 层，了解即可。
- **UB 形态**：昇腾 A5（ascend950）支持的「超节点 / Ultra Bandwidth」互联形态，README 开头明确写了「[2026/06] 增加昇腾 A5 芯片（UB 形态）支持」。UB 形态下设备间走 URMA 通信；非 UB 形态（普通 PCIe）走另一套通路。
- **HDC vs URMA 的分工**：HDC 是「主机↔设备」的同步消息底座（一套 API 支持 PCIe/Socket/UB 三种链路，最终经 ioctl 陷内核）；URMA 是「设备↔设备 / 跨进程」的内存共享与远端访问框架。两者并列存在于 `comm/` 目录下，解决不同方向的通信问题。
- **编译期宏 vs 运行期查询**：编译期宏（如 `CFG_SOC_PLATFORM_CLOUD_V2`、`ENABLE_UBE`）在 `build.sh --soc` 阶段就被冻结进二进制；运行期查询（`halSupportFeature`）在程序跑起来后按 `devId` 实时判断。一个特性到底用哪种方式，取决于它是否需要在「同一份二进制」里兼容多个形态。

> 一句话心智模型：**HDC 管「主机找设备说话」，URMA 管「设备之间共享内存」，queryfeature 管「这颗芯片到底能不能这么做」。**

## 3. 本讲源码地图

本讲涉及的关键文件如下表。核心是 `comm/ascend_urma_adapt/` 下的 6 个 `.c` 文件和 `pbl/queryfeature/` 下的 1 个 `.c` 文件。

| 文件 | 所属最小模块 | 作用 |
| --- | --- | --- |
| [ascend_urma_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_init.c) | comm/ascend_urma_adapt | 库加载时自动调用 `urma_init`，完成 URMA 框架初始化 |
| [ascend_urma_dev.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c) | comm/ascend_urma_adapt | 按「主机-设备连接类型」发现并取出 URMA 设备句柄 |
| [ascend_urma_ctx.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_ctx.c) | comm/ascend_urma_adapt | 按 devid 缓存并创建 URMA context（含引用计数） |
| [ascend_urma_token.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_token.c) | comm/ascend_urma_adapt | token 池：批量分配 / 复用 / 释放 URMA 访问令牌 |
| [ascend_urma_seg.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c) | comm/ascend_urma_adapt | segment 管理器：把一段虚拟地址注册成可远端访问的内存段 |
| [ascend_urma_raw.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_raw.c) | comm/ascend_urma_adapt | 最薄的 URMA 原语封装（token_val 获取、jfr 导入、jfc 等待） |
| [comm_user_interface.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/comm_user_interface.h) | comm/ascend_urma_adapt | 对外门面头：声明 `ascend_urma_*` 公共接口 |
| [query_feature.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c) | pbl/queryfeature | 表驱动特性查询：`halSupportFeature` 的实现 |
| [queryfeature_usr_pub_def.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/inc/queryfeature_usr_pub_def.h) | pbl/queryfeature | 日志宏适配（syslog / DRV 日志二选一） |

另外会用到的「外部佐证」文件：枚举定义 [ascend_hal_define.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h)、原型声明 [ascend_hal_base.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h)、构建开关 [queryfeature/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/CMakeLists.txt) 与 [ascend_urma_adapt/CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/CMakeLists.txt)，以及上层消费者 [svm_urma_seg_local.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/urma_adapt/urma_seg_local/svm_urma_seg_local.c)。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块逐层深入：4.1 讲 URMA 怎么初始化、怎么找到设备；4.2 讲找到设备后怎么管理 context/token/segment 这些资源；4.3 讲完全独立的另一条线——queryfeature 特性查询。

### 4.1 ascend_urma_adapt：URMA 初始化与设备发现

#### 4.1.1 概念说明

`ascend_urma_adapt` 的定位是**适配层（adapt）**，不是 URMA 本身。URMA 框架（`urma_init`、`urma_get_device_list`、`urma_create_context` 等）是外部提供的；适配层要解决的问题是：**URMA 用「设备列表 + EID」来寻址，而昇腾 HAL 用「逻辑 devid」来寻址，两者之间需要一个翻译官。**

这跟 u3-l3 讲的 UDA 角色很像——UDA 把应用视角的逻辑 devid 翻译成内核的全局设备号；这里 `ascend_urma_dev.c` 把逻辑 devid 翻译成 URMA 的 `urma_device_t *` 句柄。区别在于：UDA 走 ioctl 陷本机内核，而 URMA 的设备发现要分两种连接形态（UB 形态 vs 其他形态）走不同路径。

#### 4.1.2 核心流程

URMA 适配层的启动与设备发现流程：

```text
库加载(libascend_hal.so)
   │  constructor 自动触发
   ▼
ascend_urma_init()  ──► urma_init()              # 初始化 URMA 框架（一次性）
   │
   │  （此后上层按需调用，懒加载）
   ▼
ascend_urma_get_device(devid)                    # 翻译 devid → urma_device_t*
   │
   ├─ halGetDeviceInfo(..., INFO_TYPE_HD_CONNECT_TYPE, &type)   # 问 HAL：主机-设备怎么连的？
   │
   ├─ type == HOST_DEVICE_CONNECT_TYPE_UB ?
   │     YES → ascend_urma_get_device_when_hd_ub_conn()         # UB：走 dms_get_ub_dev_info 取内核缓存
   │     NO  → ascend_urma_get_device_when_hd_others_conn()     # 其他：遍历 urma_get_device_list
   ▼
返回 urma_device_t* + eid_index
```

关键设计点：**初始化是「自动 + 幂等」的，设备发现是「按需 + 分形态」的。** 上层模块（如 SVM）什么时候要共享内存，什么时候才调 `ascend_urma_get_device`，不会在进程启动就一股脑建好所有 context。

#### 4.1.3 源码精读

**① 库的自动初始化** —— `ascend_urma_init.c` 整个文件只有两个函数，全部靠 GCC 属性自动触发：

[ascend_urma_init.c:16-30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_init.c#L16-L30) —— `constructor` 在动态库被加载时自动调用 `urma_init`，`destructor` 在卸载时释放所有 context。注意 `urma_ret != 0` 时只打 `info` 日志而非报错，说明 URMA 初始化失败被「软容忍」——非 UB 环境下 URMA 本来就用不上，不能因为它的失败而拖垮整个 HAL 库加载。

```c
static void __attribute__((constructor)) ascend_urma_init(void)
{
    urma_init_attr_t conf = {0};
    urma_status_t urma_ret;
    urma_ret = urma_init(&conf);
    if (urma_ret != 0) {
        ascend_urma_info("Urma init check. (urma_ret=%d)\n", urma_ret);
    }
}
```

这与 u3-l2 讲的 HDC core 用 `__attribute__((constructor))` 做库自动初始化、u3-l3 讲的 UDA/URD 用 constructor 懒打开 `/dev/davinci_manager` 是同一套工程手法——**PBL/comm 各模块普遍用 constructor 实现「无显式 init 调用」的零侵入初始化。**

**② 设备发现的分形态分派** —— `ascend_urma_dev.c` 的核心是 `ascend_urma_get_device`：

[ascend_urma_dev.c:69-85](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c#L69-L85) —— 先用 `halGetDeviceInfo(devid, MODULE_TYPE_SYSTEM, INFO_TYPE_HD_CONNECT_TYPE, ...)` 查询主机-设备连接类型。这里正好呼应 u3-l1 讲过的「`halGetDeviceInfo` 是表驱动的二维（moduleType × infoType）查询接口」；也呼应 u2-l3 你新增过的 `dsmi_get_host_device_connect_type`——底层查的是同一类信息。拿到类型后，按 `HOST_DEVICE_CONNECT_TYPE_UB` 二分派：

```c
ret = halGetDeviceInfo(devid, MODULE_TYPE_SYSTEM, INFO_TYPE_HD_CONNECT_TYPE, &hd_connect_type);
...
if (hd_connect_type == HOST_DEVICE_CONNECT_TYPE_UB) {
    return ascend_urma_get_device_when_hd_ub_conn(devid, eid_index);
} else {
    return ascend_urma_get_device_when_hd_others_conn(devid, eid_index);
}
```

两条分支的实现风格截然不同：

- [ascend_urma_dev.c:25-38](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c#L25-L38) —— **UB 形态**：调一次 `dms_get_ub_dev_info(devid, &eid_info, &num)`，直接从内核缓存里取出该 devid 对应的 `urma_dev[0]` 与 `eid_index[0]`，一步到位、无遍历。
- [ascend_urma_dev.c:40-67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c#L40-L67) —— **其他形态**：调 URMA 原生 `urma_get_device_list(&num_devices)` 拿到全部设备，再 `for` 循环逐个 `urma_get_eid_list` 找第一个有有效 EID 的设备，最后 `urma_free_device_list` 释放。

> 对比体会：UB 形态下内核已经替你把「devid ↔ urma 设备」的映射缓存好了，所以是 O(1) 直取；非 UB 形态下 URMA 框架不认识昇腾 devid，只能遍历列表 O(n) 匹配。这就是适配层「屏蔽形态差异」的价值——上层永远只调 `ascend_urma_get_device(devid)`。

**③ 设备是否就绪** —— [ascend_urma_dev.c:118-138](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c#L118-L138)：`ascend_urma_dev_is_exist` 同时检查「URMA 设备存在」和「SVM 设备已初始化（`drvDeviceStatus` 返回 `DRV_STATUS_WORK`）」两个条件，缺一不可，避免在设备还没初始化完时就贸然建立 URMA 通信。

#### 4.1.4 代码实践

**实践目标**：搞清 URMA 初始化的触发时机与设备发现的分派逻辑。

**操作步骤（源码阅读型）**：

1. 打开 [ascend_urma_init.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_init.c)，确认 `ascend_urma_init` 是 `constructor`、`ascend_urma_uninit` 是 `destructor`，并且 uninit 里调的是 `ascend_urma_ctxs_release()`。
2. 打开 [ascend_urma_dev.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c)，在 `ascend_urma_get_device` 里找到 `halGetDeviceInfo(... INFO_TYPE_HD_CONNECT_TYPE ...)` 与 `HOST_DEVICE_CONNECT_TYPE_UB` 的分支。
3. 追问自己一个问题：如果某台机器既不是 UB 形态、`urma_get_device_list` 又返回 NULL，会发生什么？（看 [ascend_urma_dev.c:48-52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_dev.c#L48-L52)——返回 NULL，上层据此判断「无可用 URMA 设备」。）

**需要观察的现象 / 预期结果**：

- URMA 初始化日志形如 `Urma init check. (urma_ret=...)`，只在 `urma_ret != 0` 时出现。在**非 UB 形态**的普通 PCIe 机器上，这条 info 日志很可能出现（因为 URMA 设备根本不存在），这是预期行为、不是错误。
- 在 **UB 形态（ascend950）**机器上，`urma_init` 应返回 0，设备发现走 `ascend_urma_get_device_when_hd_ub_conn` 分支。
- 以上两条运行现象**待本地验证**（需要真实 UB 环境才能复现 UB 分支；普通开发机只能验证「非 UB 走 others 分支、urma_dev 为 NULL」）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ascend_urma_init` 里 `urma_init` 失败时只打 `info` 日志而不直接 `return` 或 `abort`？

**参考答案**：因为 `libascend_hal.so` 同时服务于 UB 形态和非 UB 形态。非 UB 环境下 URMA 框架没有可用设备，`urma_init` 返回非 0 是正常情况；若因此中断库加载，会导致整张 HAL 库在普通 PCIe 机器上无法使用。所以采用「软容忍 + info 日志」策略，把是否真正使用 URMA 的决定权推迟到上层按需调用时。

**练习 2**：`ascend_urma_get_device` 为什么要先调 `halGetDeviceInfo` 查 `INFO_TYPE_HD_CONNECT_TYPE`，而不是直接调 `urma_get_device_list`？

**参考答案**：因为 UB 形态有更快的「O(1) 直取」路径（`dms_get_ub_dev_info` 读内核缓存），只有非 UB 形态才需要回退到「O(n) 遍历 `urma_get_device_list`」。先用一次 HAL 查询判断形态，就能为 UB 形态选到更优路径，同时屏蔽掉两种形态的差异。

---

### 4.2 ascend_urma_adapt：context / token / segment 资源管理

#### 4.2.1 概念说明

找到 `urma_device_t *` 只是第一步。要让一段内存真正能被远端设备访问，URMA 还需要三类资源配合：

- **context（上下文）**：一个设备的通信句柄，由 `urma_create_context(device, eid_index)` 创建。它是后续所有操作的「门牌」。
- **token（令牌）**：远端访问内存时携带的「钥匙」，由 `urma_alloc_token_id(ctx)` 分配。同一把钥匙可以被多段内存复用以节省资源。
- **segment（内存段）**：把一段 `[start, start+size)` 的虚拟地址注册成「可被远端按 token 访问」的区域，对应 URMA 的 `urma_register_seg`。

`ascend_urma_adapt` 在这三个原语之上分别加了**缓存、池化、区间树**三层优化，这是它作为「适配层」真正提供增量价值的地方——裸 URMA 没有这些。

#### 4.2.2 核心流程

**context 的懒缓存**（[ascend_urma_ctx.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_ctx.c)）：

```text
ascend_urma_ctx_get(devid)
   │  加写锁
   ├─ g_ascend_urma_ctxs[devid] 已存在?  → uref_get 引用+1，直接返回
   ├─ 不存在? → ascend_urma_ctx_create()
   │              ├─ ascend_urma_get_device(devid)   # 复用 4.1 的设备发现
   │              └─ urma_create_context(dev, eid)    # 调 URMA 原生创建
   ├─ 存入 g_ascend_urma_ctxs[devid]，uref_get
   ▼
ascend_urma_ctx_put(ctx)  → uref_put，引用归零时回调释放
```

**token 的池化复用**（[ascend_urma_token.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_token.c)）：一个 token 池预分配 `token_num_default` 把 token，每个 token 可被 `max_acquired_num_per_token` 段内存共享；申请时优先复用旧 token（`acquire_old`），不够或要求独占（`UNIQUE` 标志）时才分配新 token；释放时若池子总量超过 `token_num_cache_up_thres` 上限才真正销毁，否则留作下次复用。

**segment 的红黑区间树**（[ascend_urma_seg.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c)）：所有已注册段按 `[start, end]` 区间挂到红黑树 `rb_root` 上，注册前先查是否与已有段重叠，重叠则按引用计数 +1（`atomic_fetch_add(&seg->ref, 1)`）或返回 `DRV_ERROR_BUSY`。虚拟地址先做页对齐再交给 URMA。

页对齐用的是 [ascend_urma_pub.h](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_pub.h) 里的 `ascend_urma_adapt_align_up/down`，原理是经典的「2 的幂对齐」位运算（要求 `align` 是 2 的幂）：

\[
\text{alignUp}(v, a) = (v + a - 1)\ \&\ \sim(a - 1)
\]

#### 4.2.3 源码精读

**① context 结构与全局缓存表** —— [ascend_urma_ctx.c:28-38](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_ctx.c#L28-L38)：

```c
struct ascend_urma_ctx {
    struct uref ref;            /* 引用计数，放到结构体首字段便于 container_of */
    uint32_t eid_index;
    urma_eid_t eid;
    urma_device_t *urma_dev;
    urma_context_t *urma_ctx;
};
static pthread_rwlock_t g_rwlock = PTHREAD_RWLOCK_INITIALIZER;
static struct ascend_urma_ctx *g_ascend_urma_ctxs[ASCEND_URMA_MAX_DEV_NUM] = {NULL};
```

`ASCEND_URMA_MAX_DEV_NUM` 定义在对外头 [comm_user_interface.h:18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/inc/comm_user_interface.h#L18) 为 `65U`（64 颗芯片 + 1 host 槽位）。读写锁 `g_rwlock` 保护这张「以 devid 为下标」的缓存表。

**② 懒创建 + 引用计数** —— [ascend_urma_ctx.c:81-109](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_ctx.c#L81-L109)：先查缓存，命中则 `uref_get` 直接返回；未命中才 `ascend_urma_ctx_create`，创建内部调 `ascend_urma_get_device`（复用 4.1 的设备发现）+ `urma_create_context`。这是典型的「双检锁懒初始化 + 引用计数」模式，和 u3-l3 UDA 的双检锁、u3-l4 commlib 的 CAS 自旋锁同属 PBL 的并发基础设施套路。

**③ segment 注册：建配置 + 调 URMA** —— [ascend_urma_seg.c:77-109](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L77-L109)：把 `seg_flag` 里的位（`ACCESS_WRITE` / `PIN` / `WITHOUT_TOKEN_VAL`）翻译成 URMA 的 `seg_cfg.flag.bs.*` 字段，地址按页对齐，token 从 token 池取，最后 `urma_register_seg(ctx, &seg_cfg)`。`access` 字段三选一很关键：

```c
if (ascend_urma_seg_flag_is_access_write(seg_flag)) {
    seg_cfg.flag.bs.access = URMA_ACCESS_READ | URMA_ACCESS_WRITE | URMA_ACCESS_ATOMIC;
} else {
    seg_cfg.flag.bs.access = URMA_ACCESS_READ;
}
```

**④ segment 管理器装配** —— [ascend_urma_seg.c:360-380](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L360-L380)：`ascend_urma_seg_mng_create` 先建 token 池、再建挂载红黑树的 segment 管理器，把「token 池 + 区间树」打包成一个对象返回给上层。这是 `ascend_urma_adapt` 暴露给 SVM 的最高层入口。

**⑤ 真实消费者：SVM** —— [svm_urma_seg_local.c:97-121](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/urma_adapt/urma_seg_local/svm_urma_seg_local.c#L97-L121)：SVM 在设备初始化阶段调 `ascend_urma_seg_mng_create`，为每个 devid 建两个管理器（`g_seg_mng` 和 `g_self_user_seg_mng`），配置 token 池参数（host 侧默认 64 把 token、每把最多 512 段共享）。后续 `halMemRegister` 类操作最终经 `svm_urma_seg_local_register` → `ascend_urma_register_seg` 落到本层。这条链路把本讲和单元 4（SVM）串了起来。

#### 4.2.4 代码实践

**实践目标**：验证「上层一次内存注册」如何穿过 token 池 + 红黑树到达 URMA 原生接口。

**操作步骤（源码阅读 + 局部参数观察）**：

1. 从消费者 [svm_urma_seg_local.c:131-147](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/urma_adapt/urma_seg_local/svm_urma_seg_local.c#L131-L147) 的 `svm_urma_seg_local_register` 出发，跟踪到 [ascend_urma_seg.c:391-400](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L391-L400) 的 `ascend_urma_register_seg`。
2. 进入 [ascend_urma_seg.c:199-249](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L199-L249) 的 `ascend_urma_seg_register`，注意三段逻辑：①查重叠（`ascend_urma_get_seg`）②按是否与已有段重叠决定 token 是否要求 `UNIQUE`（[L220-L222](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L220-L222)）③从池里取 token（`ascend_urma_token_acquire`）。
3. **改参数观察**：把 [svm_urma_seg_local.c:24-29](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/v3/urma_adapt/urma_seg_local/svm_urma_seg_local.c#L24-L29) 的 `SVM_URMA_TOKEN_DEFAULT_NUM`（host 侧 64）想象成改成 1，推断会对 token 池行为产生什么影响——token 更频繁地达到 `max_acquired_num_per_token` 上限，从而更频繁地分配新 token、走 `ascend_urma_token_acquir_new`。

**需要观察的现象 / 预期结果**：

- 正常情况下同一地址区间重复注册会命中 `ascend_urma_get_seg` 的「已存在」分支，`seg->ref` 递增、不会重复 `urma_register_seg`（性能优化点）。
- 区间真正重叠但起止不完全相同时返回 `DRV_ERROR_BUSY`（[ascend_urma_seg.c:209-218](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L209-L218)）。
- 上述运行行为**待本地验证**（需要 UB / ascend950 环境才能触发真实注册路径）。

#### 4.2.5 小练习与答案

**练习 1**：`ascend_urma_ctx_get` 为什么要用读写锁（`pthread_rwlock_t`）而不是普通互斥锁？

**参考答案**：context 表是「读多写少」——一旦某个 devid 的 context 建好，后续大量请求都只是命中缓存读取（`uref_get`），只有首次未命中才需要写（创建并插入）。读写锁允许多个读并发、只对写互斥，比普通互斥锁并发性更好。

**练习 2**：`ascend_urma_seg_register` 里，什么情况下会要求新分配的 token 带上 `ASCEND_URMA_TOKEN_FLAG_UNIQUE` 标志？

**参考答案**：当待注册区间的页对齐范围与已注册段存在重叠（`ascend_urma_exist_registered_seg_in_range` 返回 true）时，会加上 `UNIQUE` 标志（[ascend_urma_seg.c:220-222](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L220-L222)）。`_ascend_urma_token_acquire` 见到 `UNIQUE` 就跳过复用旧 token、直接分配新 token，避免共享同一把钥匙引发访问冲突。

**练习 3**：`ascend_urma_seg` 结构里 `atomic_int ref` 的作用是什么？

**参考答案**：对同一段内存的多次注册做引用计数。`register` 时 +1、`unregister` 时 -1；只有当引用计数降到 0（最后一个使用者释放）时才真正 `urma_unregister_seg` 并归还 token（见 [ascend_urma_seg.c:260-281](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_seg.c#L260-L281)）。这避免了在还有其他使用者时提前销毁段。

---

### 4.3 pbl/queryfeature：表驱动特性查询

#### 4.3.1 概念说明

现在切换到完全独立的第二条线。driver 是「一份代码支持多芯片」的：`build.sh --soc=ascend910b` 和 `--soc=ascend950` 编出来的是两份不同的二进制（靠编译期宏区分），但即便在同一份二进制里，有些特性还要看**运行时的设备状态**才能定——比如某特性只在「未做算力切分（normal 模式）」时可用。

`pbl/queryfeature` 就是给上层提供一个统一问句接口：

```c
bool halSupportFeature(uint32_t devId, drvFeature_t type);
```

它解决的问题是：**把「某个特性是否支持」的判断逻辑收敛到一处，避免散落在各模块的 `if/else` 里。** 这样新增一个特性只需加一个判断函数 + 一行表注册，调用方代码完全不用改结构。

#### 4.3.2 核心流程

queryfeature 的设计是教科书级的「表驱动 + 函数指针」：

```text
调用方: halSupportFeature(devId, FEATURE_XXX)
   │
   ├─ 边界校验: 0 <= type < FEATURE_MAX ?
   ├─ 查表: g_feature_support[type]  (函数指针表，以枚举为下标)
   ├─ 表项为 NULL ? → 返回 false（该特性未注册 = 不支持）
   ▼
调用 g_feature_support[type](devId) → 返回 true/false
```

每个 `FEATURE_XXX` 枚举值对应一个 `bool (*)(uint32_t devId)` 形态的判断函数。判断函数内部可以混合三种依据：

1. **编译期宏**（如 `CFG_FEATURE_GET_QOS_MASTER_CFG`）——`#ifdef` 决定，编出来就固定。
2. **编译期宏 + 平台宏**（如 `CFG_SOC_PLATFORM_CLOUD_V2`）——按芯片形态定。
3. **运行期设备状态**（如 `halGetDeviceSplitMode`）——按设备实时切分模式定。

> 关键洞见：**编译期宏的「值」在运行期不可见（宏在预处理后就消失了），但宏可以决定「编译进哪个函数体」。所以 queryfeature 用「函数指针表」把编译期宏的决策结果「搬运」到运行期可查询的布尔值。** 这就是为什么明明有编译期宏，还要再套一层运行期查询——为了让上层用同一份调用代码（`halSupportFeature`）拿到结果。

#### 4.3.3 源码精读

**① 特性枚举** —— [ascend_hal_define.h:1612-1629](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h#L1612-L1629)：`drvFeature_t` 枚举从 0 开始编号，最后用 `FEATURE_MAX` 做哨兵（既是表大小、又是越界检查依据）。注意有些枚举项带注释说明前置条件，如 `FEATURE_SVM_MEM_REGISTER_QUERY_AND_GET_ATTR = 11` 要求「先调一次 halSupportFeature 启用，再调 svm register 接口」。

**② 函数指针类型与表** —— [query_feature.c:21, 100-112](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L100-L112)：

```c
typedef bool (*feature_support_handle)(uint32_t devId);
static const feature_support_handle g_feature_support[FEATURE_MAX] = {
    [FEATURE_TRSDRV_SQ_DEVICE_MEM_PRIORITY] = featureSupportTsDrvSqDevmemPrio,
    [FEATURE_PROF_AICPU_CHAN] = featureSupportProfAicpuChan,
    [FEATURE_SVM_GET_USER_MALLOC_ATTR] = svm_support_get_user_malloc_attr,
    /* ... 其余项 ... */
    [FEATURE_APM_RES_MAP_REMOTE] = featureSupportApmResMapRemote,
};
```

这是 C 语言「指定初始化器」（designated initializer）写法：用 `[枚举名] = 函数名` 直接把函数挂到对应下标，没写的下标自动为 NULL。新增一个特性只要加一行，**已有项的顺序、下标都不会被打乱**——这比 switch/case 安全得多（switch 改顺序容易漏）。

**③ 判断函数的三种风格** —— 同一个文件里能看到三种判断依据并存：

- **纯编译期宏**：[query_feature.c:73-81](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L73-L81) `featureSupportDmsGetQosMasterConfig`，`#ifdef CFG_FEATURE_GET_QOS_MASTER_CFG` 决定 true/false。
- **编译期宏 + 嵌套**：[query_feature.c:51-71](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L51-L71) `featureSupportProfAicpuChan`，外层先看 `CFG_FEATURE_PROF_AICPU_CHAN_DEFUALT`，没有定义才回退到运行期 `halGetDeviceSplitMode` 判断。
- **纯运行期查询**：[query_feature.c:29-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L29-L43) `featureSupportTsDrvSqDanamicBind`，调 `halGetDeviceSplitMode(devId, &mode)`，只有 `VMNG_NORMAL_NONE_SPLIT_MODE`（未切分）才返回 true。
- **委托给其他模块**：表中 `FEATURE_SVM_GET_USER_MALLOC_ATTR` 直接指向 SVM 模块的 `svm_support_get_user_malloc_attr`——queryfeature 不实现判断逻辑，只做「路由」。

**④ 入口函数** —— [query_feature.c:114-125](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L114-L125)：

```c
bool halSupportFeature(uint32_t devId, drvFeature_t type)
{
    if (type < 0 || type >= FEATURE_MAX) {
        return false;
    }
    if (g_feature_support[type] != NULL) {
        return (g_feature_support[type])(devId);
    } else {
        return false;     /* 未注册的特性一律视为不支持 */
    }
}
```

对外原型声明在 [ascend_hal_base.h:5740](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L5740)，带 `DLLEXPORT` 导出，是 HAL 对外的公共接口之一。其 `@attention` 文档（见 base.h 的 Doxygen 注释）还会提醒调用方：某些特性需要「先调一次 halSupportFeature 启用，再调对应业务接口」。

**⑤ 编译期宏从哪来** —— [queryfeature/CMakeLists.txt:32-39](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/CMakeLists.txt#L32-L39)：`CFG_SOC_PLATFORM_CLOUD_V2`、`CFG_FEATURE_GET_QOS_MASTER_CFG` 等宏由 CMake 按 `${PRODUCT}` 注入。比如 ascend910B 会同时定义 `CFG_SOC_PLATFORM_CLOUD` 与 `CFG_SOC_PLATFORM_CLOUD_V2`。这与 u1-l2 讲的「`build.sh --soc` 经 get_product 选定 PRODUCT、再驱动 driver_config.cmake」一脉相承——**编译期宏的源头就是 `--soc` 参数。**

而 [query_feature.h:14-18](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.h#L14-L18) 把其中一条宏进一步加工成布尔常量：

```c
#if defined(CFG_SOC_PLATFORM_CLOUD_V2) && defined(DRV_HOST)
#define TRSDRV_SQ_DEVICE_MEM_PRIORITY_SUPPORT true
#else
#define TRSDRV_SQ_DEVICE_MEM_PRIORITY_SUPPORT false
#endif
```

`featureSupportTsDrvSqDevmemPrio` 直接 `return TRSDRV_SQ_DEVICE_MEM_PRIORITY_SUPPORT;`（[query_feature.c:23-27](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L23-L27)），把编译期宏包成函数，从而能进函数指针表。

#### 4.3.4 代码实践

**实践目标**：动手「新增一个特性」，打通「编译期宏 / 运行期查询 → 表注册 → 上层调用」全链路。

**操作步骤（源码阅读 + 设计型，不改源码也可完成）**：

1. **选依据**：假设要新增特性 `FEATURE_MY_NEW_FEATURE`，先决定它的判断依据属于三种里的哪一种。这里选「运行期查询」——只有设备处于 normal（未切分）模式才支持。
2. **加枚举**：在 [ascend_hal_define.h:1612-1629](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_define.h#L1612-L1629) 的 `drvFeature_t` 里、`FEATURE_MAX` 之前加一行 `FEATURE_MY_NEW_FEATURE = 13,`。
3. **写判断函数**：参照 [query_feature.c:29-43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L29-L43) 的 `featureSupportTsDrvSqDanamicBind`，写一个同形态函数（调 `halGetDeviceSplitMode`，返回 `mode == VMNG_NORMAL_NONE_SPLIT_MODE`）。
4. **注册到表**：在 [query_feature.c:100-112](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/query_feature.c#L100-L112) 的 `g_feature_support` 里加一行 `[FEATURE_MY_NEW_FEATURE] = myFeatureSupportFunc,`。
5. **上层调用**：在任何要使用该特性的地方写 `if (halSupportFeature(devId, FEATURE_MY_NEW_FEATURE)) { ... }`。

**需要观察的现象 / 预期结果**：

- 不改枚举只加表项会编译失败（枚举不存在）；不加表项只加枚举则 `halSupportFeature` 对该 type 返回 false（表项为 NULL）——验证「枚举与表项必须配对」。
- 在切分模式下调用应返回 false，normal 模式下返回 true。
- 上述运行结果**待本地验证**（依赖真实设备与切分模式状态）。

> 这是「源码阅读型实践」：在不修改源码的前提下，通过设计完整的新增步骤，验证你对表驱动机制的理解。若要真正编译验证，需在本机或容器按 u1-l2 的 `bash build.sh --pkg --soc=ascend910b` 流程重编（属于破坏性改动，请在分支上操作）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `g_feature_support` 表用「指定初始化器 `[FEATURE_XXX] = func`」而不是「按顺序写 `func1, func2, ...`」？

**参考答案**：指定初始化器把「枚举值」和「函数」显式绑定，新增 / 删除 / 调整枚举顺序时不会因为位置错位而把函数挂到错误的下标；未显式赋值的下标自动置 NULL（即「不支持」），语义清晰。按顺序写则强依赖枚举顺序与表顺序完全一致，极易在维护中出错。

**练习 2**：`halSupportFeature` 里 `type < 0` 这个判断对 `drvFeature_t`（枚举，底层通常是 unsigned int）有意义吗？

**参考答案**：严格来说意义不大——枚举一般按 unsigned 处理，不会小于 0，这行更多是防御性编程。真正起作用的边界是 `type >= FEATURE_MAX`，它防止越界访问 `g_feature_support` 数组。保留 `type < 0` 是稳健起见，成本为零。

**练习 3**：`FEATURE_SVM_GET_USER_MALLOC_ATTR` 的表项指向 `svm_support_get_user_malloc_attr`，而不是在本文件里实现判断。这种「委托」设计有什么好处？

**参考答案**：好处是**关注点分离**——SVM 相关的判断逻辑（哪些情况下能查 malloc 属性）只有 SVM 模块自己最清楚，应由 SVM 实现；queryfeature 只负责「路由 + 边界校验 + 统一入口」。这样 SVM 内部逻辑变化不会扩散到 queryfeature，queryfeature 也不必理解每个特性的业务细节。代价是 queryfeature 编译时要能链接到 SVM 提供的符号。

---

## 5. 综合实践

**任务**：把本讲两条主线串起来，画一张「从 build.sh 到一次跨设备内存共享」的完整适配链路图，并用一段话解释 queryfeature 在其中扮演的「守门员」角色。

**具体步骤**：

1. **编译期（左半边）**：从 `bash build.sh --soc=ascend950 --ube` 出发，追踪它如何经 `get_product` 设定 `PRODUCT=ascend950`，再由 [ascend_urma_adapt/CMakeLists.txt:18-33](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/CMakeLists.txt#L18-L33)（`host` + `ENABLE_UBE` 才编译 urma_adapt 的 `.c`）和 [queryfeature/CMakeLists.txt:32-39](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/pbl/queryfeature/CMakeLists.txt#L32-L39)（注入 `CFG_*` 宏）冻结进二进制。注意 ascend950 + aarch64 还会额外定义 `SSAPI_USE_MAMI`（[ascend_urma_adapt/CMakeLists.txt:77](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/CMakeLists.txt#L77)），它会影响 raw 层 `ascend_urma_import_jfr` 走 MAMI 分支（[ascend_urma_raw.c:103-110](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_raw.c#L103-L110)）。
2. **运行期（右半边）**：进程启动 → `ascend_urma_init` constructor 调 `urma_init` → SVM 初始化时 `ascend_urma_seg_mng_create` 建 token 池 + 红黑树 → 应用注册共享内存 → `ascend_urma_register_seg` → `urma_register_seg`。
3. **守门员**：在链路的关键节点上标出「先问 queryfeature」的位置。例如，SVM 在决定是否走「register 查询属性」这条路径前，会先 `halSupportFeature(devId, FEATURE_SVM_MEM_REGISTER_QUERY_AND_GET_ATTR)`（见 [ascend_hal_base.h:2614-2618](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/pkg_inc/ascend_hal_base.h#L2614-L2618) 的 `@attention`）；只有返回 true 才启用该路径。

**预期产出**：一张包含「编译期宏注入」「constructor 自动初始化」「设备发现分形态」「资源管理三件套」「queryfeature 守门」五个标注的链路图，配一段说明：queryfeature 用运行期布尔查询把编译期宏的决策结果暴露给上层，让同一份上层代码能安全地在多芯片 / 多形态下运行。

> 运行验证**待本地验证**（需 ascend950 / UB 形态真机）。在普通开发机上，本任务可作为纯源码阅读 + 文档产出完成。

## 6. 本讲小结

- `comm/ascend_urma_adapt` 是把昇腾 HAL 的「逻辑 devid」适配到外部 URMA RDMA 框架的薄胶水层，负责设备发现、context 缓存、token 池化、segment 红黑区间树四件事——其中后三件是它相对裸 URMA 的增量价值。
- URMA 初始化靠 `constructor` 自动触发且**软容忍失败**（非 UB 环境 `urma_init` 失败只打 info 日志），设备发现按 `INFO_TYPE_HD_CONNECT_TYPE` 分 UB（O(1) 直取内核缓存）与其他（O(n) 遍历设备列表）两条路径。
- context 用「以 devid 为下标的数组 + 读写锁 + 引用计数（uref）」懒缓存；segment 用红黑树管理区间重叠、用 `atomic_int ref` 支持多注册引用、用 token 池复用访问令牌；上层消费者是 SVM（`svm_urma_seg_local.c`）。
- `pbl/queryfeature` 用「函数指针表 + 枚举下标」的表驱动设计实现 `halSupportFeature(devId, type)`，把判断逻辑收敛到一处；新增特性只需「加枚举 + 写函数 + 注册一行」。
- 特性判断函数内部可混合三种依据：纯编译期宏（如 `CFG_FEATURE_GET_QOS_MASTER_CFG`）、编译期宏 + 平台宏（如 `CFG_SOC_PLATFORM_CLOUD_V2`）、纯运行期设备状态（如 `halGetDeviceSplitMode`）；宏的源头是 `build.sh --soc` 经 CMake 注入。
- **双层适配**思想：编译期宏（`--soc` → CMake → `CFG_*`）决定「这份二进制支持什么」，运行期 `halSupportFeature` 决定「当前这颗芯片 / 这个时刻能不能用」，两者配合实现一份代码多芯片兼容。

## 7. 下一步学习建议

- **进入单元 4（SVM）**：本讲的 `ascend_urma_seg_mng_create` 消费者就在 SVM。建议接着读 u4-l1（SVM 总览与初始化）和 u4-l4（内存拷贝与共享机制），看 `halShmemCreateHandle` / `halMemRegister` 如何最终落到本讲的 segment 注册。
- **回头看 HDC**：若想对比「主机-设备通信」与「设备-设备通信」的差异，重读 u3-l2 的 HDC client/server/core 模型，体会两者同为 `comm/` 下通信底座、却解决不同方向问题的设计。
- **多芯片构建细节**：u8-l3（多芯片适配与构建特性配置）会从构建系统角度更系统地讲 `feature_config.cmake` / `driver_config.cmake` 与 `--soc`、`--ube` 的关系，是对本讲「编译期宏从哪来」的延伸。
- **raw 层深入**：若你对 RDMA 原语（jetty / JFC / JFR / TP）感兴趣，可精读 [ascend_urma_raw.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/comm/ascend_urma_adapt/ascend_urma_raw.c) 的 `ascend_urma_import_jfr` 与 `ascend_urma_wait_jfc`（含 EINTR 重试与超时重算），这是适配层最贴近 URMA 协议的部分。
