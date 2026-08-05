# 插件入口：注册机制与发现流程

## 1. 本讲目标

上一讲（u1-l2《源码地图》）你已经看过 `vllm_ascend/` 的目录结构，并在 4.3 节里瞥见过一条「发现链路」：`setup.py` 的 entry points → `register()` → `"vllm_ascend.platform.NPUPlatform"` → `NPUPlatform` 类。那讲里我们**故意没有展开**，而是把它留给了本讲。

本讲就来彻底讲透这条链路。读完本讲，你将能够：

1. 说清楚 vLLM 是如何通过 **entry points（入口点）** 发现并加载 `vllm-ascend` 这个插件的——从 `vllm serve`/`LLM(...)` 启动那一刻，到 `NPUPlatform` 被选中成为「当前平台」，中间发生了什么。
2. 掌握 `register()` 为什么返回一个**字符串路径** `"vllm_ascend.platform.NPUPlatform"`，而不是直接返回类，以及这种「延迟 import」写法的意义。
3. 理解 `vllm-ascend` 登记的**五把注册钩子**（`register` / `register_connector` / `register_model_loader` / `register_service_profiling` / `register_model`）分别在什么时候被调用、各自负责注册什么。
4. 理解 `_ensure_global_patch()` 为什么是一个「幂等闸门」、为什么必须在引擎核心子进程里重新打补丁，以及文件顶部的 **triton 兼容桩**（gluon / `_aggregate`）解决的是什么问题。

本讲是「把插件和 vLLM 连起来」的那根线。掌握了它，你才能在后续单元（u3 Patch、u4 Worker、u9 模型加载）里看懂每一段代码是在哪个进程、哪个时机被触发的。

## 2. 前置知识

读本讲前，请确认你已建立以下直觉（来自 u1-l1 ~ u1-l4）：

- **硬件插件（Hardware Pluggable）**：vLLM 不把硬件代码写死，而是留出接口，让外部包来填。`vllm-ascend` 就是这样一个外部包，它本身不 `import` 进用户代码——是 vLLM 主动发现它、加载它。
- **entry points（入口点）**：Python 打包标准里的机制。一个包在元数据里登记 `组名 = 模块:函数`，别的程序启动时扫描某个组，就能找到并调用插件登记的函数。
- **`NPUPlatform`**：`vllm-ascend` 提供给 vLLM 的「平台身份证」，告诉 vLLM「我这是一张昇腾卡」。
- **进程模型**：在线服务 `vllm serve` 会 fork/spawn 出**引擎核心子进程（engine-core）**和若干 **worker 子进程**；离线推理 `LLM(...)` 在多卡时也会 spawn worker。理解「补丁在哪个进程生效」是本讲的关键。

再补充两个本讲会用到的概念：

- **import 的副作用（import side effect）**：在 Python 里，`import` 一个模块会**执行该模块顶层代码**。`vllm-ascend` 大量利用这一点——很多初始化（打补丁、配置日志、塞 triton 桩）都写在模块顶层或 `__init__.py` 里，只要被 import 就会自动执行，调用方无需显式触发。这是理解本讲的核心思维模型。
- **engine-core 子进程**：vLLM v1 架构里，真正跑调度器（Scheduler）和引擎核心（EngineCore）的进程。它和用户进程不是同一个，很多在用户进程里通过测试 `conftest.py` 打的补丁，**在这里不会生效**——这正是 `_ensure_global_patch()` 要解决的问题。

> 术语提示：本讲的「插件」「平台插件」「硬件插件」指的是同一件事——`vllm-ascend` 这个 Python 包。

## 3. 本讲源码地图

本讲只聚焦「插件如何被发现与注册」，涉及以下文件：

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| `setup.py` | 打包脚本，含 `entry_points` 字典 | 讲五把钩子如何登记给 vLLM |
| `vllm_ascend/__init__.py` | 插件入口，定义全部 `register*()` 回调 | 讲每把钩子的实现、`_ensure_global_patch`、triton 桩 |
| `vllm_ascend/platform.py` | `NPUPlatform` 类，含 `pre_register_and_update` | 讲平台被选中后如何触发平台级补丁 |
| `vllm_ascend/utils.py` | 含 `adapt_patch()` 补丁分发函数 | 讲两阶段补丁（platform / worker）的分发 |
| `vllm_ascend/patch/__init__.py` | Patch 总模块的文档说明 | 讲平台级 vs worker 级补丁的时机约定 |
| `vllm_ascend/distributed/kv_transfer/__init__.py` | KV 连接器注册 | 讲 `register_connector` 注册了哪些连接器 |
| `vllm_ascend/logger.py` | 日志配置 | 讲入口文件末尾 `import logger` 的副作用 |

## 4. 核心概念与源码讲解

### 4.1 插件入口：entry points 发现机制

#### 4.1.1 概念说明

「插件」要起作用，必须先回答两个问题：

1. **vLLM 怎么知道世界上存在一个叫 `vllm-ascend` 的插件？**
2. **vLLM 怎么知道这个插件提供的平台类是哪一个？**

答案都落在 Python 的 **entry points** 机制上。简单说：

- `vllm-ascend` 在自己的打包元数据（`setup.py` 的 `entry_points`）里登记一条记录：在名为 `vllm.platform_plugins` 的组里，有一个叫 `ascend` 的入口，指向 `vllm_ascend` 包的 `register` 函数。
- 当你 `pip install` 这个包后，这条记录就写进了包的元数据里。
- vLLM 启动时，用标准库 `importlib.metadata` 去扫描 `vllm.platform_plugins` 这个组，找到所有登记过的插件，**逐个 import 它们的模块、调用它们的 `register` 函数**。

`register` 函数的职责很轻：它**不直接返回平台类**，而是返回一个**字符串**——平台类的「模块路径」。vLLM 拿到字符串后，自己再去 import 这个类、实例化它、把它设为「当前平台（`current_platform`）」。

为什么要绕这一步用字符串，而不是 `return NPUPlatform`？这是 vLLM 可插拔硬件接口的约定：**延迟 import**。如果 `register()` 里直接 `from vllm_ascend.platform import NPUPlatform`，那么 vLLM 一发现插件、还没决定要不要用它，就会触发一长串重型 import（torch、torch-npu、CANN……），既拖慢启动，又容易引发循环依赖。返回字符串把这个代价推迟到「真正要选平台」的那一刻。

#### 4.1.2 核心流程

从你敲下 `vllm serve ...` 或运行 `LLM(...)` 开始，到 `NPUPlatform` 被选中，核心流程是：

```
1. vLLM 启动，初始化平台层
2. 扫描 entry points 组 "vllm.platform_plugins"
3. 找到 ascend = vllm_ascend:register
4. import vllm_ascend  ← 触发 __init__.py 顶层代码（triton 桩、日志配置）
5. 调用 vllm_ascend.register()
6. register() 返回字符串 "vllm_ascend.platform.NPUPlatform"
7. vLLM 延迟 import 该字符串指向的类 → 得到 NPUPlatform
8. vLLM 选中 NPUPlatform 作为 current_platform
9. （后续）vLLM 调用平台的钩子，如 pre_register_and_update()
```

注意第 4 步：**import 本身就是有副作用的**。`vllm_ascend/__init__.py` 在被 import 的瞬间，会执行顶层的 triton 兼容桩、配置日志。这意味着「插件被发现」这一刻，很多兼容性修补就已经悄悄生效了——这正是「import 副作用」思维的体现。

#### 4.1.3 源码精读

**① 入口点登记**：`setup.py` 的 `setup()` 调用里，`entry_points` 字典把五把钩子登记给了 vLLM 的两个组。

参见 [setup.py:543-551](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L543-L551) ——关键是第一行：`"vllm.platform_plugins": ["ascend = vllm_ascend:register"]`。这条登记告诉 vLLM「平台插件组里有一个 `ascend`，入口是 `vllm_ascend` 包的 `register` 函数」。其余四条属于 `vllm.general_plugins` 组，我们在 4.2 节展开。

**② `register()` 返回字符串路径**：插件入口文件里，`register` 的实现只有一行。

参见 [vllm_ascend/__init__.py:73-76](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L73-L76) ——`def register(): return "vllm_ascend.platform.NPUPlatform"`。它不 import、不实例化，只交出一个字符串，把真正的 import 时机交给 vLLM。

**③ 入口文件顶层的 import 副作用**：当 vLLM 为了调用 `register()` 而 import `vllm_ascend` 时，`__init__.py` 的顶层代码会先被执行。其中两段尤其重要（本节先点到，4.3 节精读）：

- triton 兼容桩（构造 `gluon` 模块、补 `_aggregate`），见 [vllm_ascend/__init__.py:22-51](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L22-L51)。
- 文件末尾配置日志的 `import vllm_ascend.logger`，见 [vllm_ascend/__init__.py:119](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L119) ——它给 `vllm_ascend` 这个 logger 命名空间挂上带 `[vllm-ascend]` 前缀的 handler（实现在 `logger.py` 的 `configure_ascend_logging`，细节不影响本讲主线）。

**④ 平台类的真实存在**：`register()` 返回的字符串指向的类，确实定义在 `platform.py` 里。

参见 [vllm_ascend/platform.py:127-128](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L127-L128) ——`class NPUPlatform(Platform):` 继承自 vLLM 的 `Platform`，并用 `_enum = PlatformEnum.OOT` 把自己登记为「带外（out-of-tree）平台」。这个类的具体钩子方法留到 u2-l1 精读。

#### 4.1.4 代码实践

这是一个**可运行的验证型实践**（在已安装 `vllm_ascend` 的环境里），兼带源码追踪。目标：亲眼看到「entry points 确实登记了 ascend 插件」。

1. **实践目标**：用 Python 标准库读出 `vllm-ascend` 登记的 entry points，验证 vLLM 能「发现」它。
2. **操作步骤**：
   - 在已 `pip install` 过 `vllm_ascend` 的环境里，运行下面这段「示例代码」：
     ```python
     # 示例代码：枚举 vllm-ascend 登记给 vLLM 的所有入口点
     from importlib.metadata import entry_points

     for group in ("vllm.platform_plugins", "vllm.general_plugins"):
         eps = entry_points(group=group)
         for ep in eps:
             if ep.value.startswith("vllm_ascend"):
                 print(group, "->", ep.name, "=", ep.value)
     ```
   - 若环境里**没有安装** `vllm_ascend`（只读源码），则改为直接打开 [setup.py:543-551](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L543-L551)，逐行抄出五个 `名字 = 值` 对。
3. **需要观察的现象**：你会看到一条 `vllm.platform_plugins -> ascend = vllm_ascend:register`，以及四条 `vllm.general_plugins -> ... = vllm_ascend:register_*`。
4. **预期结果**：确认「平台插件组」里只有 `ascend = vllm_ascend:register` 这一条与 ascend 相关，且它的值是 `模块:函数` 形式。若环境未安装，则「待本地验证」运行结果，但源码里的登记内容是确定的。
5. 提醒：`ep.value` 形如 `vllm_ascend:register`，冒号前是模块、冒号后是函数名——这正是 vLLM 调用 `register()` 的依据。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `setup.py` 里 `"ascend = vllm_ascend:register"` 这一行删掉，会发生什么？

**参考答案**：vLLM 在扫描 `vllm.platform_plugins` 组时将**找不到 ascend 插件**，于是不会 import `vllm_ascend`、不会调用 `register()`、也不会选中 `NPUPlatform`。结果就是 vLLM 退化成默认平台（CUDA/CPU），在 NPU 机器上跑不起来或行为异常。entry points 是插件被发现的唯一入口，删掉等于「让插件隐身」。

**练习 2**：为什么 `register()` 返回字符串 `"vllm_ascend.platform.NPUPlatform"`，而不是 `from vllm_ascend.platform import NPUPlatform; return NPUPlatform`？

**参考答案**：为了**延迟 import**。直接 import 会立刻触发 `platform.py` 顶层的一长串依赖（torch、torch-npu、vllm 内部模块等），既拖慢插件发现阶段，也可能引发循环依赖。返回字符串把这个代价推迟到 vLLM「真正决定要使用该平台」时，由 vLLM 自己按需 import。这也是 vLLM 可插拔硬件接口的通用约定。

---

### 4.2 注册回调家族：五把钩子各司其职

#### 4.2.1 概念说明

`vllm-ascend` 登记的不止「平台」这一把钩子。在 `setup.py` 里，它向 vLLM 的**两个组**共登记了**五条入口**：

| 组 | 入口名 | 回调函数 | 一句话职责 |
| --- | --- | --- | --- |
| `vllm.platform_plugins` | `ascend` | `register` | 告诉 vLLM「我的平台类是 `NPUPlatform`」 |
| `vllm.general_plugins` | `ascend_kv_connector` | `register_connector` | 注册 NPU 版 KV 连接器与权重传输引擎 |
| `vllm.general_plugins` | `ascend_model_loader` | `register_model_loader` | 注册 RFork / Netloader 等自定义模型加载器 |
| `vllm.general_plugins` | `ascend_service_profiling` | `register_service_profiling` | 生成服务化 profiling 配置 |
| `vllm.general_plugins` | `ascend_model` | `register_model` | 注册 `vllm-ascend` 自带的新模型/处理器兼容层 |

理解这两组的区别是本节的关键：

- **`vllm.platform_plugins`（平台插件）**：vLLM 在**平台层初始化**阶段处理。回调返回平台类路径，vLLM 据此选中平台。这是「身份登记」。
- **`vllm.general_plugins`（通用插件）**：vLLM 把它们当作「启动时要调用的函数」。回调**返回值不重要**，重要的是回调**执行时产生的副作用**——往 vLLM 的各种注册表里塞 NPU 版实现。这些回调主要在**引擎核心子进程**里被调用。

换句话说：`register` 决定「用谁当平台」，其余四把钩子决定「往这个平台周围补充哪些 NPU 专属组件」。

#### 4.2.2 核心流程

五把钩子被调用的时机并不相同：

```
用户进程（vllm serve / LLM(...)）
  │
  ├─ 平台层初始化
  │    └─ 扫描 vllm.platform_plugins → 调 register() → 得到 NPUPlatform 路径
  │       └─ （稍后）NPUPlatform.pre_register_and_update() 被调用
  │            └─ 内部 adapt_patch(is_global_patch=True) → 应用平台级补丁
  │
  └─ 拉起 engine-core 子进程（spawn，全新解释器）
       │
       └─ engine-core 里扫描 vllm.general_plugins → 依次调用：
            ├─ register_connector()        → 注册 KV 连接器 + 权重传输引擎
            ├─ register_model_loader()     → 注册 RFork / Netloader
            ├─ register_service_profiling()→ 生成 profiling 配置
            └─ register_model()            → 注册新模型 + 处理器兼容层
            （每个回调内部都会先 _ensure_global_patch()，保证平台级补丁在本进程也生效）
```

这里有一个容易忽略的细节：`general_plugins` 是在 **engine-core 子进程**里被加载的（见 4.3 节 `_ensure_global_patch` 的注释）。而每个回调的第一行几乎都是 `_ensure_global_patch()`——这不是巧合，下一节会解释为什么。

#### 4.2.3 源码精读

**① 五条入口的登记**：再细看 `entry_points` 字典，把它和回调一一对应。

参见 [setup.py:543-551](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L543-L551) ——`vllm.platform_plugins` 组只有一条 `ascend = vllm_ascend:register`；`vllm.general_plugins` 组有四条，分别指向 `register_connector`、`register_model_loader`、`register_service_profiling`、`register_model`。

**② `register_connector`：注册 KV 连接器与权重传输引擎**。

参见 [vllm_ascend/__init__.py:79-86](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L79-L86) ——先 `_ensure_global_patch()`，再分别从 `kv_transfer` 和 `weight_transfer` 子模块取出 `register_connector` 与 `register_engine` 并调用。它把 NPU 版的 PD 分离连接器（mooncake、ascend_store、lmcache 等）塞进 vLLM 的 `KVConnectorFactory`，把 HCCL/IPC 权重传输引擎塞进 `WeightTransferEngineFactory`。其中 `kv_transfer.register_connector()` 注册了一长串连接器，详见 [vllm_ascend/distributed/kv_transfer/__init__.py:21-87](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/distributed/kv_transfer/__init__.py#L21-L87)（这块的细节留到 u10-l2《PD 分离与 KV 传输连接器》）。

**③ `register_model_loader`：注册自定义加载器**。

参见 [vllm_ascend/__init__.py:89-96](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L89-L96) ——调用 `register_netloader()` 和 `register_rforkloader()`，把 RFork（进程 fork 快速加载）和 Netloader（弹性网络加载）登记进 vLLM 的加载器注册表（细节留到 u9《模型加载与权重传输》）。

**④ `register_service_profiling` 与 `register_model`**。

参见 [vllm_ascend/__init__.py:99-104](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L99-L104) ——调用 `generate_service_profiling_config()` 生成服务化性能采集配置。

参见 [vllm_ascend/__init__.py:107-116](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L107-L116) ——先安装 HunyuanVL 处理器兼容层（`install_hunyuan_vl_processor_compat`），再调用 `models.register_model()` 注册 `vllm-ascend` 自带的新模型（如 deepseek_v4、minimax_m3 等，留到 u11-l1）。

**⑤ 平台选中后的「平台级补丁」入口**：`NPUPlatform` 被选中后，vLLM 会调用它的 `pre_register_and_update` 钩子，这里才是平台级补丁真正落地的位置。

参见 [vllm_ascend/platform.py:182-203](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L182-L203) ——方法体第一件事就是 `adapt_patch(is_global_patch=True)`，紧接着把 `ascend` 加进 `--quantization` 的可选值、按是否 310P 注册量化配置类。注意：平台级补丁在这里触发，而不是在 `register()` 里——因为 `register()` 只返回字符串、不执行重型逻辑，真正「动手」要等到平台类被实例化、钩子被调用。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，目标是把「五把钩子 → 各自注册了什么」对应清楚。

1. **实践目标**：填一张「钩子职责表」，确认每个回调的真实调用对象。
2. **操作步骤**：
   - 打开 [vllm_ascend/__init__.py:79-116](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L79-L116)，对 `register_connector`、`register_model_loader`、`register_service_profiling`、`register_model` 四个函数，分别记录「它 import 了哪个子模块、调用了哪个 register 函数」。
   - 对 `register_connector`，进一步跳进 [vllm_ascend/distributed/kv_transfer/__init__.py:21-87](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/distributed/kv_transfer/__init__.py#L21-L87)，数一数它一共调用了几次 `KVConnectorFactory.register_connector(...)`。
3. **需要观察的现象**：四个回调里，**每一个的第一行都是 `_ensure_global_patch()`**（`register_model` 是例外，它不打全局补丁，只做模型注册）。
4. **预期结果**：你能复述「`register_connector` 注册连接器+权重引擎、`register_model_loader` 注册两种加载器、`register_service_profiling` 生成 profiling 配置、`register_model` 注册新模型+处理器兼容层」。
5. 思考题（不必写答案）：为什么 `register_model` 不需要调 `_ensure_global_patch()`？（提示：它注册的是模型类，不依赖平台级补丁改写过的调度器/引擎代码。）

#### 4.2.5 小练习与答案

**练习 1**：`vllm.platform_plugins` 和 `vllm.general_plugins` 这两组，vLLM 对它们的处理方式有什么本质区别？

**参考答案**：对 `platform_plugins`，vLLM 把回调返回值（平台类路径字符串）当作「身份」——据此选中平台；回调本身应当轻量。对 `general_plugins`，vLLM 把回调当作「带副作用的初始化函数」——返回值被忽略，重点是执行时往 vLLM 的各注册表（连接器、加载器、模型等）里塞 NPU 版实现。前者是「我是谁」，后者是「我往你身上装哪些零件」。

**练习 2**：为什么 `register_connector`、`register_model_loader`、`register_service_profiling` 都要在第一行调用 `_ensure_global_patch()`？

**参考答案**：因为这些回调运行在 **engine-core 子进程**里，而该子进程是 spawn 出来的全新解释器，父进程打过的补丁不会继承。为了让平台级补丁（影响调度器、引擎核心的补丁）在这个子进程里也生效，必须借这些「必然会在子进程里被调用」的入口重新打一次；而 `_ensure_global_patch()` 的幂等设计保证多次调用只真正执行一次。

---

### 4.3 幂等闸门与 triton 兼容桩：让补丁只打一次、import 不报错

#### 4.3.1 概念说明

本节解决两个「看不见但很要命」的问题：

**问题一：补丁会在子进程里丢失，但又不能重复打。**

vLLM v1 的进程模型里，调度器和引擎核心跑在 **engine-core 子进程**。在线服务用 `spawn` 方式拉起它，意味着这是一个**全新的 Python 解释器**——父进程里打过的 monkey-patch（比如改写了某个调度器方法）在这里**全部失效**。而端到端测试里那种靠 `conftest.py` 在测试进程打补丁的做法，在 engine-core 子进程里也**不会执行**。

怎么办？`vllm-ascend` 的办法是：**借通用插件回调「顺路」在子进程里重新打补丁**。因为 `vllm.general_plugins` 的回调正是在 engine-core 子进程里被 vLLM 调用的。于是每个回调都调一次 `_ensure_global_patch()`。

但这又带来新问题：有四个回调，难道要打四次补丁？重复打补丁会「套娃」——把已经改写过的对象再改写一遍，行为不可预测。于是需要一把**幂等闸门**：用模块级标志位 `_GLOBAL_PATCH_APPLIED` 保证「每个进程里只打一次」。

**问题二：import vllm_ascend 的瞬间就会因为 triton 缺东西而崩。**

vLLM 主干代码会 `from triton.experimental import gluon`，并调用 `triton.language.core._aggregate`。但 `vllm-ascend` 依赖的 `triton-ascend 3.2.1` 并不提供这些。更要命的是，vLLM 为了发现插件，会**先 import `vllm_ascend`**（去解析 `register()`），而这个 import 发生在任何 `from triton.experimental import gluon` **之前**——包括在 `python -m vllm.model_executor.models.registry` 这样的子进程里。

解决办法依然是利用 import 副作用：在 `__init__.py` 的**最顶层**，趁自己被 import 的瞬间，往 `sys.modules` 里塞两个假的 `gluon` 模块、给 `triton.language.core` 补一个 `_aggregate`。这样后续 vLLM 主干的 import 就不会因为缺这些而报错。

#### 4.3.2 核心流程

**幂等闸门 `_ensure_global_patch` 的流程**：

```
某 general_plugins 回调被调用（在 engine-core 子进程）
  │
  └─ 调 _ensure_global_patch()
       │
       ├─ 若 _GLOBAL_PATCH_APPLIED 已为 True → 直接 return（本进程已打过）
       │
       └─ 否则：
            ├─ adapt_patch(is_global_patch=True)
            │    └─ import vllm_ascend.patch.platform  ← 触发平台级补丁应用
            └─ _GLOBAL_PATCH_APPLIED = True  ← 上锁，后续调用直接跳过
```

**两阶段补丁的分工**（来自 `patch/__init__.py` 的文档）：

```
adapt_patch(is_global_patch=True)  → import vllm_ascend.patch.platform
    └─ 平台级补丁：在 worker 启动前应用，影响调度器、引擎核心、分布式等
       触发点：NPUPlatform.pre_register_and_update() / _ensure_global_patch()

adapt_patch(is_global_patch=False) → import vllm_ascend.patch.worker
    └─ worker 级补丁：在每个 worker 启动时应用，影响模型前向、算子替换等
       触发点：每个 worker 的 __init__
```

**triton 兼容桩的流程**：

```
vLLM 为解析 register() 而 import vllm_ascend
  │
  └─ 执行 __init__.py 顶层代码（在任何 gluon import 之前）
       ├─ 若 VLLM_VERSION != "0.26.0"：
       │    ├─ 往 sys.modules 塞假模块 triton.experimental.gluon
       │    ├─ 往 sys.modules 塞假模块 triton.experimental.gluon.language
       │    └─ 若 triton 已安装且无 _aggregate：
       │         给 triton.language.core 补一个 no-op 的 _aggregate
       └─ （继续执行后续顶层代码，包括 _ensure_global_patch 的定义、register 的定义等）
```

#### 4.3.3 源码精读

**① 幂等闸门 `_ensure_global_patch`**：模块级标志位 + 惰性 import。

参见 [vllm_ascend/__init__.py:53-70](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L53-L70) ——`_GLOBAL_PATCH_APPLIED = False` 是闸门状态；函数体里 `if _GLOBAL_PATCH_APPLIED: return` 是短路；`from vllm_ascend.utils import adapt_patch` 写在函数内部（惰性 import，避免循环依赖）；`adapt_patch(is_global_patch=True)` 是真正动手；最后置标志位为 `True`。docstring 明确说明了设计动机：「vLLM 在 engine-core 子进程里加载通用插件，E2E 测试的 conftest 钩子在那里不运行，所以影响调度器和引擎代码的全局补丁必须通过这些插件入口来补上」。

**② 两阶段补丁的分发**：`adapt_patch` 靠 import 副作用工作。

参见 [vllm_ascend/utils.py:533-537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537) ——`is_global_patch=True` 时 import `vllm_ascend.patch.platform`，否则 import `vllm_ascend.patch.worker`。函数体本身什么逻辑都没有——**补丁的应用全靠 import 子包时的副作用**。这种「import 即应用」的写法是 `vllm-ascend` 的标志性风格。

**③ 两阶段补丁的时机约定**：`patch/__init__.py` 顶部有一段珍贵的设计文档。

参见 [vllm_ascend/patch/__init__.py:17-27](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L17-L27) ——它明确写了：`platform/` 目录的补丁「在 worker 启动前应用」，由 `adapt_patch(is_global_patch=True)` 在 `NPUPlatform.pre_register_and_update()` 里触发；`worker/` 目录的补丁「在 worker 启动时应用」，由 `adapt_patch(is_global_patch=False)` 在每个 worker 的 `__init__` 里触发。并且要求：**每新增一个补丁，都要在这个文件里补一段描述**（What/Why/How/Related PR/Future Plan），这也是 u3-l1 将精读的「补丁规范」。

**④ triton 兼容桩**：入口文件最顶层的 import 副作用。

参见 [vllm_ascend/__init__.py:22-51](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L22-L51) ——第 22 行先用 `importlib.util.find_spec("triton")` 探测 triton 是否存在（`_triton_available`）；第 30 行用 `if os.getenv("VLLM_VERSION", "") != "0.26.0":` 跳过 0.26.0 这个特例；第 33-38 行把两个 gluon 模块路径塞进 `sys.modules` 成空 `ModuleType`（这样上游 `from triton.experimental import gluon` 不会 `ImportError`）；第 44-51 行在 triton 已安装但缺 `_aggregate` 时，给它补一个 no-op。注释里点明了关键时机：「Runs at module-import time, which is triggered by vllm.platforms plugin discovery」——也就是说，正是 vLLM 发现插件、import `vllm_ascend` 这一步，让这些兼容桩抢在所有 gluon import 之前生效，连子进程（如 `python -m vllm.model_executor.models.registry`）也覆盖到了。

#### 4.3.4 代码实践

这是一个**追踪+推理型实践**，无需 NPU，目标理解「幂等」与「import 副作用」。

1. **实践目标**：论证「即使四个 general_plugins 回调都调用 `_ensure_global_patch()`，平台级补丁在每个进程里也只应用一次」。
2. **操作步骤**：
   - 阅读 [vllm_ascend/__init__.py:53-70](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L53-L70)，标出「读标志位」「打补丁」「写标志位」三步。
   - 假设有如下「示例代码」调用顺序（模拟 engine-core 子进程里的四次回调）：
     ```python
     # 示例代码：模拟四个回调先后触发
     from vllm_ascend import _ensure_global_patch
     for _ in range(4):           # 对应四个 general_plugins 回调
         _ensure_global_patch()
     ```
   - 思考：`adapt_patch(is_global_patch=True)` 实际执行了几次？
3. **需要观察的现象**：第一次调用时 `_GLOBAL_PATCH_APPLIED` 为 `False`，会真正执行 `adapt_patch(...)` 并把标志位置 `True`；后三次调用都在入口处 `return`，`adapt_patch` 不会再被调用。
4. **预期结果**：`adapt_patch` 在单个进程内**只执行一次**，其余三次被短路。这就是「幂等」的含义——多次调用、单次生效。若你想在真机验证，可在 `adapt_patch` 入口加一行 `print`（属于修改源码，仅用于学习，验证后请还原）观察打印次数——具体次数「待本地验证」。
5. 进阶思考：为什么 `from vllm_ascend.utils import adapt_patch` 写在函数体内部，而不是写在文件顶部？（提示：避免在 import `vllm_ascend` 时就触发 `utils` → `platform` 等一长串重型 import，符合「延迟 import」原则。）

#### 4.3.5 小练习与答案

**练习 1**：`_GLOBAL_PATCH_APPLIED` 是模块级变量。在 `spawn` 出的 engine-core 子进程里，它的初始值是什么？为什么这不会导致「补丁没打」？

**参考答案**：`spawn` 会启动一个**全新解释器**并重新 import `vllm_ascend`，因此 `_GLOBAL_PATCH_APPLIED` 在子进程里的初始值仍是 `False`（定义处 [vllm_ascend/__init__.py:53](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L53)）。正因为是 `False`，子进程里第一次调用 `_ensure_global_patch()` 才会真正执行 `adapt_patch`，把平台级补丁在子进程里重新打一遍。也就是说：**幂等是「每进程一次」，不是「全局一次」**——这正是子进程补丁能生效的原因。

**练习 2**：triton 兼容桩为什么要写在 `__init__.py` 的**最顶层**，而不是写在 `register()` 函数里？

**参考答案**：因为 vLLM 发现插件时是 **import `vllm_ascend` 模块**，顶层代码在 import 瞬间就执行了；而 `register()` 要等 vLLM 显式调用才执行。上游的 `from triton.experimental import gluon` 可能在 import `vllm_ascend` 之后、`register()` 之前就发生（甚至在模型注册子进程里）。只有把桩写在顶层，才能保证它在**所有 gluon import 之前**抢先生效。这也再次体现了「import 副作用」的时序敏感性。

**练习 3**：`adapt_patch` 的函数体里没有任何 `if/for`，只有一行 `import`。这种「import 即打补丁」的写法，依赖的是什么 Python 机制？

**参考答案**：依赖 **import 的副作用**——`import vllm_ascend.patch.platform` 会执行该子包 `__init__.py` 的全部顶层代码，而那些顶层代码（以及它们进一步 import 的各 `patch_*.py` 模块）里就包含着「把上游某方法替换成 NPU 版」的 monkey-patch 语句。`adapt_patch` 只负责「按阶段选择 import 哪个子包」，真正干活的是 import 触发的模块加载。

## 5. 综合实践

把本讲三块知识串起来，完成「插件发现→register→平台选中」的**时序草图**任务。这是本讲规格里指定的实践。

1. **实践目标**：画出一张时序图（或文字版时序表），把 vLLM 启动到 `NPUPlatform` 被选中、平台级补丁生效的全过程表达清楚，并标注每一步发生在「哪个进程」。
2. **操作步骤**：
   - 准备两栏：左栏「用户/父进程」，右栏「engine-core 子进程」。
   - 按下面顺序在两栏间画箭头（可参考 4.1.2 与 4.2.2 的流程）：
     1. 父进程：vLLM 启动 → 扫描 `vllm.platform_plugins` → 找到 `ascend = vllm_ascend:register`。
     2. 父进程：import `vllm_ascend`（标注：「此处触发 triton 兼容桩 + 日志配置」）。
     3. 父进程：调用 `register()` → 返回字符串 `"vllm_ascend.platform.NPUPlatform"`。
     4. 父进程：vLLM 延迟 import 该字符串 → 得到 `NPUPlatform` → 选中为 `current_platform`。
     5. 父进程：vLLM 调用 `NPUPlatform.pre_register_and_update()` → 内部 `adapt_patch(is_global_patch=True)`（标注：「平台级补丁首次应用」）。
     6. 父进程 → engine-core 子进程：spawn 拉起子进程（全新解释器）。
     7. 子进程：扫描 `vllm.general_plugins` → 依次调用四个回调；每个回调先 `_ensure_global_patch()`（标注：「幂等闸门，子进程内首次→真正打补丁；后续→短路」）。
   - 在图旁用一句话回答：为什么子进程里要重新打补丁？为什么不会重复打？
3. **需要观察的现象**：你会发现「平台级补丁」在父进程的 `pre_register_and_update` 里打了一次，又在子进程的 `_ensure_global_patch` 里打了一次——这是**两个不同进程各打一次**，而不是同一进程打两次。
4. **预期结果**：得到一张清晰标注「进程边界」的时序图，能据此向别人讲清楚 `register`、`pre_register_and_update`、`_ensure_global_patch` 三者各自在什么进程、什么时机触发。
5. 提示：如果你能在 NPU 环境跑 `vllm serve`，可结合启动日志核对各步骤顺序（日志文案「待本地验证」）；若没有 NPU，纯按源码画出时序图即可，本实践不依赖运行。

## 6. 本讲小结

- vLLM 通过 **entry points** 发现 `vllm-ascend`：`setup.py` 在 `vllm.platform_plugins` 组登记 `ascend = vllm_ascend:register`，vLLM 启动时扫描该组、import 插件、调用 `register()`。
- `register()` 返回**字符串路径** `"vllm_ascend.platform.NPUPlatform"` 而非类本身，这是 vLLM 可插拔硬件接口的「延迟 import」约定，用于避开早期重型 import 与循环依赖。
- `vllm-ascend` 共登记**五把钩子**：`register`（平台插件组，决定平台身份）和 `register_connector` / `register_model_loader` / `register_service_profiling` / `register_model`（通用插件组，靠副作用注册 NPU 版连接器、加载器、profiling 配置、新模型）。
- **import 副作用**是本插件的核心思维模型：`__init__.py` 顶层的 triton 兼容桩和日志配置、`adapt_patch` 里「import 即打补丁」，都依赖 import 触发顶层代码执行。
- `_ensure_global_patch()` 是一把**每进程一次的幂等闸门**：因为 engine-core 子进程是 spawn 出的全新解释器、父进程补丁不继承，所以借通用插件回调在子进程里重新打补丁；`_GLOBAL_PATCH_APPLIED` 标志保证同一进程内不重复打。
- **两阶段补丁**：`adapt_patch(is_global_patch=True)` import `patch.platform`（worker 启动前、影响调度/引擎），`adapt_patch(is_global_patch=False)` import `patch.worker`（每个 worker 启动时、影响模型前向）。

## 7. 下一步学习建议

本讲把「插件如何被发现与注册」讲透了，但有意没有进入任何被注册对象的内部。建议按以下顺序继续：

1. **先读 u2-l1《NPUPlatform：平台核心能力》**：本讲只用到 `NPUPlatform.pre_register_and_update` 这一个钩子，u2-l1 会把 `NPUPlatform` 重写的全部关键方法（`check_and_update_config`、`get_attn_backend_cls`、`get_compile_backend` 等）讲清楚。
2. **再读 u3-l1《Patch 机制总览与两阶段应用》**：本讲多次提到 `adapt_patch` 的两阶段分发，u3 会进入 `patch/platform/` 与 `patch/worker/` 内部，讲每个补丁的 What/Why/How/Future Plan 规范。
3. **按需跳读**：如果你想看 `register_connector` 注册的那些连接器细节，跳到 u10-l2；想看 `register_model_loader` 注册的 RFork/Netloader，跳到 u9-l2、u9-l3；想看 `register_model` 注册的新模型，跳到 u11-l1。

读这些后续讲义时，请记住本讲建立的「进程边界 + import 副作用 + 幂等闸门」三件套——它们会反复出现，是你判断「这段代码为什么在这里执行」的底层依据。
