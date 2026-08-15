# LLM-DataDist Python 接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `llm_datadist` Python 包的目录结构、公开导出清单，以及它与底层 C++ wrapper（`llm_datadist_wrapper.so`）的关系。
2. 掌握 `LLMDataDist`、`LLMConfig`、`CacheManager` 等 Python 类的用法：初始化、配置生成、建链、Cache 分配/注册、Push/Pull。
3. 读懂 `pull_cache_sample.py`（同步 Pull）与 `transfer_cache_async_sample.py`（异步分层传输）两个样例的组织方式，并能对比两者的接口差异。
4. 学会查阅 `docs/zh/api/python/` 下的 Python API 文档定位接口语义。

本讲承接 u6-l1 建立的 LLM-DataDist 全景认知（角色模型、CacheIndex 寻址、PD 分离场景），把视角从 C++ 公开接口切换到 Python 侧的「薄封装层」。

## 2. 前置知识

- **Pimpl 的 Python 对应物**：u2-l1 讲过 C++ 公开类用 Pimpl 隐藏实现；Python 侧的 `LLMDataDist` 同样只是外壳，真正的逻辑在 `llm_datadist_wrapper.so`（C++ 扩展模块）里，Python 类负责参数校验、类型翻译和异常转换。
- **CacheManager 模式**：Python 的 `CacheManager` 前置条件是初始化选项 `llm.EnableCacheManager=1`（或配置了 `llm.LocalCommRes`）。这对应 u6-l1 中「LLM-DataDist 的 Cache 管理路径」。
- **远端 Cache 直访（remote_cache_accessible）**：开启 `llm.EnableRemoteCacheAccessible=1` 后，Push/Pull 走「C2C 直推」路径（`push_cache`/`push_blocks`/`transfer_cache_async`），这正是 u6-l7 讲过的 HIXL 传输后端所服务的场景。
- **torch_npu**：样例用 `torch` + `.npu()` 分配设备内存，再取 `tensor.data_ptr()`（一个整数地址）传给 LLM-DataDist 注册。Python 侧与 C++ 侧传递的都是「裸设备地址整数」，这一点与 u7-l1 的 HIXL Python 绑定一致。
- **gloo 进程组**：两个样例都用 `torch.distributed`（gloo 后端）的 `barrier()` 做双进程同步——LLM-DataDist 本身不提供跨进程通知，这是业务侧自建控制面的典型做法（u6-l4 也强调过这一点）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/python/llm_datadist/llm_datadist/__init__.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/__init__.py) | 包入口：汇总导出约 20 个公开符号，并对 v1 独有类做懒加载 |
| [src/python/llm_datadist/llm_datadist/v2/llm_datadist.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_datadist.py) | `LLMDataDist` 主类：init/finalize/link/unlink/switch_role |
| [src/python/llm_datadist/llm_datadist/v2/cache_manager.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py) | `CacheManager`：allocate/register/pull/push/transfer_cache_async 等 Cache 操作 |
| [src/python/llm_datadist/llm_datadist/v2/llm_types.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py) | 数据类型层：CacheDesc/Cache/CacheKey/TransferConfig/CacheTask 等 |
| [src/python/llm_datadist/llm_datadist/v2/llm_utils.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py) | 工具层：pack_* 打包函数与异步分层传输线程模型 |
| [src/python/llm_datadist/llm_datadist/configs.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/configs.py) | `LLMRole`/`LLMClusterInfo`/`LlmConfig`（配置生成器） |
| [src/python/llm_datadist/llm_datadist/status.py](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/status.py) | `LLMStatusCode`/`LLMException` 错误码与异常体系 |
| [src/python/llm_datadist/CMakeLists.txt](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/CMakeLists.txt) | whl 打包：把纯 Python 包与两个 .so 打成 llm_datadist-0.0.1 whl |
| [examples/python/pull_cache_sample.py](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/pull_cache_sample.py) | 同步 Pull 样例（allocate_cache + pull_cache，HCCL link 路径） |
| [examples/python/transfer_cache_async_sample.py](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/transfer_cache_async_sample.py) | 异步分层传输样例（register_blocks_cache + transfer_cache_async） |
| [docs/zh/api/python/](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/python/README.md) | Python API 参考文档（每个类一个 md 文件） |

> 注意一个容易混淆的点：`src/python/llm_datadist/__init__.py`（外层）是**空文件**，真正的包在嵌套目录 `src/python/llm_datadist/llm_datadist/` 里。外层目录只是 setup.py 的工程根。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：

1. **模块一**：llm_datadist Python 包结构与 whl 打包。
2. **模块二**：`LLMDataDist` 主类与 `LLMConfig` 配置生成。
3. **模块三**：`CacheManager` 与核心数据类型。
4. **模块四**：异步分层传输（`transfer_cache_async` 样例精读）。

### 4.1 llm_datadist Python 包结构与 whl 打包

#### 4.1.1 概念说明

`llm_datadist` 是一个「纯 Python 外壳 + C 扩展内核」的混合包：

- **纯 Python 层**（`v2/`、`configs.py`、`status.py` 等）：参数校验、类型打包、异常翻译、异步线程编排。
- **C 扩展层**（`llm_datadist_wrapper.so`、`metadef_wrapper.so`）：pybind11 风格的 C++ wrapper，直接对接 `src/llm_datadist/` 的实现（u6-l1/u6-l2 讲过的 `LlmDataDistImpl`/`LLMDataDistV2`）。

与 u7-l1 的 `hixl` 裸扩展模块不同，`llm_datadist` 走 **whl 流水线**：CMake 把两个 .so 拷进纯 Python 目录后用 `setup.py bdist_wheel` 打包。

#### 4.1.2 核心流程

包的导入与打包流程：

```text
import llm_datadist
  └─ __init__.py 顶层导出（LLMDataDist/CacheDesc/CacheKey/...）
       └─ v2/llm_datadist.py 等模块 import llm_datadist_wrapper（.so）
            └─ C++ LLMDataDistV2 实现（u6-l2）

构建期：
  CMake 目标 llm_datadist_python
    └─ 拷贝 setup.py + llm_datadist/ + 两个 .so 到 wheel1/
         └─ python setup.py bdist_wheel
              └─ llm_datadist-0.0.1-py3-none-any.whl
```

#### 4.1.3 源码精读

包入口汇总导出所有公开符号，并对三个 v1 独有类做懒加载，避免无谓地加载旧模块：

[src/python/llm_datadist/llm_datadist/__init__.py:L38-L67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/__init__.py#L38-L67) —— 从 `v2` 子包导入 `LLMDataDist`，从 `llm_types` 导入全部数据类，组装 `__all__`。这就是 Python 用户 `from llm_datadist import ...` 时能拿到的全部名字。

[src/python/llm_datadist/llm_datadist/__init__.py:L97-L116](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/__init__.py#L97-L116) —— 模块级 `__getattr__` 懒加载：只有用户真正访问 `TensorDesc`/`Tensor`/`KvCacheManager`（v1 遗留接口）时才 `from llm_datadist_v1 import ...`，其余名字直接抛 `AttributeError`。这是 Python 3.7+ 的标准懒加载惯用法。

[src/python/llm_datadist/CMakeLists.txt:L12-L26](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/CMakeLists.txt#L12-L26) —— whl 打包命令：把 `setup.py`、`MANIFEST.in`、纯 Python 目录和两个 wrapper .so 汇到 `wheel1/` 临时目录再 `bdist_wheel`。产物 `llm_datadist-0.0.1-py3-none-any.whl` 随 install 目标发布（u1-l2 构建讲义中 build_out 目录可见）。

错误码体系直接复用 wrapper 模块的常量，保持 Python/C++ 两侧同值：

[src/python/llm_datadist/llm_datadist/status.py:L19-L48](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/status.py#L19-L48) —— `LLMStatusCode` 的每个枚举值都取自 `llm_wrapper.kXxx`（即 .so 里的常量），因此与 u6-l1 讲过的 C++ 错误码（0x5010Bxxx 段的 `ge::` 枚举）一一对应；未知错误统一映射 `LLM_UNKNOWN_ERROR = -1`。

[src/python/llm_datadist/llm_datadist/status.py:L127-L130](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/status.py#L127-L130) —— `handle_llm_status`：所有 wrapper 调用的统一出口——返回码非 `kSuccess` 就抛 `LLMException`（携带 status_code）。这就是「Python 侧不用判返回值、靠异常编程」的机制来源。

#### 4.1.4 代码实践

1. **实践目标**：确认包内可导入的公开符号清单与文档目录一一对应。
2. **操作步骤**：
   - 构建安装 whl（需已加载 CANN 环境，参见 u1-l2）：`pip install build_out/llm_datadist-0.0.1-py3-none-any.whl`；
   - 在 Python 里执行 `import llm_datadist; print(llm_datadist.__all__)`；
   - 对照 [docs/zh/api/python/README.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/python/README.md) 的目录列表做映射表。
3. **需要观察的现象**：`__all__` 中的每个类都能在文档目录下找到同名 md 文件；访问 `llm_datadist.KvCacheManager`（v1 类）不会在 import 时报错，只在访问时触发二次导入。
4. **预期结果**：得到一张「Python 符号 → API 文档 → 所属源码文件」三列映射表。若无 NPU 环境无法安装 whl，则改为纯源码阅读：直接对照 `__init__.py` 的 import 语句与文档目录。**待本地验证**（本环境无昇腾硬件与已装 whl）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TensorDesc`/`Tensor`/`KvCacheManager` 不直接放在 `__init__.py` 顶部 import？

**答案**：它们属于 v1 遗留接口，位于独立的 `llm_datadist_v1` 模块。顶部 import 会让所有用户（包括只用 v2 的用户）都加载 v1 模块及其依赖；用模块级 `__getattr__` 懒加载，只有真正访问这三个名字时才付出加载代价，源码注释也写明「只有外部用例真正使用 v1 里独有的类时才会触发 import」。

**练习 2**：`hixl` Python 模块（u7-l1）和 `llm_datadist` 的发布形态有何不同？

**答案**：`hixl` 是裸扩展模块 `hixl.so`，由安装脚本直接复制部署；`llm_datadist` 是「纯 Python 包 + 两个 .so」打成的 whl（`llm_datadist-0.0.1-py3-none-any.whl`），由 CMake 自定义命令在构建期用 `setup.py bdist_wheel` 生成。

### 4.2 LLMDataDist 主类与 LLMConfig 配置生成

#### 4.2.1 概念说明

`LLMDataDist` 是 Python 侧的入口类，接口面比 C++ 的 `LlmDataDist`（u6-l1）略窄，但把「配置生成」也收了进来：

- `LLMConfig`（configs.py 中的 `LlmConfig`，导出时同时给了 `LLMConfig` 别名）用属性风格收集配置，`generate_options()` 一次性翻译成 `Dict[str, str]` 引擎选项——用户不必手写字符串键。
- `LLMDataDist.init()` 根据选项里是否启用 CacheManager，把调用路由到 v2 wrapper（`initialize_v2`）或 v1 wrapper，并在 v2 模式下创建 `CacheManager`。
- 全进程**单例**约束：类属性 `llm_engine_instance` 保证同一进程只能初始化一个实例。

#### 4.2.2 核心流程

初始化与建链的调用序列（v2 / CacheManager 模式）：

```text
LLMConfig().device_id / enable_cache_manager / listen_ip_info / ...
    ↓ generate_options()          # 属性 → Dict[str,str]
LLMDataDist(role, cluster_id).init(options)
    ↓ _setup_engine_option()      # 补默认：llm.Role、LocalCommRes 联动开关、listenIpInfo 拆分
    ↓ initialize_v2(cluster_id, options)   # 进 C++ wrapper（u6-l2 的两层 Pimpl 管线）
    ↓ CacheManager(wrapper, options)       # v2 模式专属
link_clusters([LLMClusterInfo], timeout)   # 新式建链（ip:port 交换）
    或 link(comm_name, rank_info, rank_table)  # 旧式 HCCL rank table 建链
```

#### 4.2.3 源码精读

[src/python/llm_datadist/llm_datadist/v2/llm_datadist.py:L82-L127](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_datadist.py#L82-L127) —— `init()` 的双路由：启用 CacheManager 时走 `initialize_v2` 并创建 `CacheManager`；否则回退 v1 的 `initialize` 并创建 `KvCacheManager`。末尾 `LLMDataDist.llm_engine_instance = self` 落单例标记。

[src/python/llm_datadist/llm_datadist/v2/llm_datadist.py:L419-L459](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_datadist.py#L419-L459) —— `_setup_engine_option` 的选项补齐逻辑，三个要点：① 自动注入 `llm.Role`；② 配置了 `llm.LocalCommRes` 时默认打开 `llm.EnableCacheManager=1` 与 `llm.EnableRemoteCacheAccessible=1`（联动默认值）；③ `llm.TransferBackend`（HIXL 后端，见 u6-l7）强校验：必须开 CacheManager 与 RemoteCacheAccessible，且必须配置 `llm.listenIpInfo`。`llm.listenIpInfo`（`ip:port` 字符串）还会被 `parse_listen_ip_info` 拆成 `llm.ListenIp`/`llm.ListenPort` 两个独立键。

[src/python/llm_datadist/llm_datadist/configs.py:L145-L173](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/configs.py#L145-L173) —— `LlmConfig.gen_options()`：把 device_id、sync_kv_timeout、内存池、链路等属性翻译成引擎选项字典。`device_id` 会同时写入 `ge.exec.deviceId` 与 `ge.session_device_id`（多卡时用分号拼接）。

[src/python/llm_datadist/llm_datadist/configs.py:L416-L448](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/configs.py#L416-L448) —— `_add_memory_options`/`_add_connect_options`：布尔属性转 `"1"/"0"` 字符串，字符串属性（如 `mem_pool_cfg` 的 JSON、`transfer_backend`）直接透传，最终产出 `llm.MemPoolConfig`、`llm.TransferBackend` 等 u6 系列讲义中反复出现的选项键。

[src/python/llm_datadist/llm_datadist/v2/llm_datadist.py:L259-L299](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_datadist.py#L259-L299) —— `link_clusters`：v2 模式下把每个 `LLMClusterInfo` 打包成 `(remote_cluster_id, 0, local_ip_info_list, remote_ip_info_list)` 元组列表调 `link_clusters_v2`；返回值是 `(总状态, [每集群状态])`，注意这里返回的是 `code_2_status` 翻译后的 `LLMStatusCode` 而**不抛异常**——与 `init` 的异常风格不同，需要调用方判 `ret != LLMStatusCode.LLM_SUCCESS`。

[src/python/llm_datadist/llm_datadist/v2/llm_datadist.py:L468-L480](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_datadist.py#L468-L480) —— `atexit` 兜底：进程退出时若实例未 finalize，自动补一次 `finalize()`（吞掉重复 finalize 的异常）。这保证异常退出的进程也尽量释放引擎资源。

#### 4.2.4 代码实践

1. **实践目标**：用 `LLMConfig` 生成一份完整选项字典，验证属性→选项键的翻译规则。
2. **操作步骤**（纯 Python，不需要 NPU，但需要已安装 whl；无 whl 时把下面代码当作源码阅读练习，对照 configs.py 手工推演）：

   ```python
   # 示例代码：仅演示 LLMConfig 用法，未在真实环境运行
   from llm_datadist import LLMConfig

   cfg = LLMConfig()
   cfg.device_id = 0
   cfg.enable_cache_manager = True
   cfg.enable_remote_cache_accessible = True
   cfg.listen_ip_info = "192.168.1.10:26000"
   cfg.transfer_backend = "hixl"
   cfg.mem_pool_cfg = '{"memory_size": 67108864}'
   print(cfg.generate_options())
   ```

3. **需要观察的现象**：输出的 dict 中出现 `ge.exec.deviceId=0`、`llm.EnableCacheManager=1`、`llm.EnableRemoteCacheAccessible=1`、`llm.listenIpInfo=192.168.1.10:26000`、`llm.TransferBackend=hixl`、`llm.MemPoolConfig=...` 六个键。
4. **预期结果**：对照 `_add_memory_options`/`_add_connect_options` 的源码逐键核对；注意 `llm.listenIpInfo` 在 `LLMConfig` 层保持原样，拆分成 `llm.ListenIp`/`llm.ListenPort` 发生在 `LLMDataDist._setup_engine_option`（需要真正调 `init` 才触发）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`link_clusters` 和 `init` 的错误处理风格有什么不同？为什么？

**答案**：`init` 内部调用 `handle_llm_status`，失败直接抛 `LLMException`；`link_clusters` 返回 `(LLMStatusCode, List[LLMStatusCode])` 由调用方判断。因为批量建链可能「部分集群成功、部分失败」，抛异常会丢失部分成功的信息，返回逐集群状态更合适（样例里就是 `ret, _ = datadist.link_clusters(...)` 后自行 raise）。

**练习 2**：如果用户只配置了 `llm.TransferBackend="hixl"` 而没开 `enable_cache_manager`，会发生什么？

**答案**：`_setup_engine_option` 中 `raise_if_false(self._enable_cache_mgr, ...)` 会抛出 `LLMException`（`LLM_PARAM_INVALID`），提示 `llm.TransferBackend is not supported when llm.EnableCacheManager is not configured`；此外还会强校验 `EnableRemoteCacheAccessible=1` 与 `llm.listenIpInfo` 存在。这三个条件正是 u6-l7 讲的 HIXL 后端适配器 `HixlTransferEngine` 的门槛。

### 4.3 CacheManager 与核心数据类型

#### 4.3.1 概念说明

`CacheManager` 是 Python 侧 KV Cache 的操作面（u6-l3 Cache 管理机制的 Python 门面）。获得 Cache 有两条路径，与 u6-l3 的结论一致：

- **Register 路径**：`register_cache`/`register_blocks_cache`——把 torch_npu 张量的 `data_ptr()` 地址列表注册进来（`is_registered=True`）。
- **Allocate 路径**：`allocate_cache`/`allocate_blocks_cache`——从 `llm.MemPoolConfig` 配置的内存池切分（要求先配池）。

寻址用三类 key（对应 u6-l1 的 CacheIndex 三级寻址）：

| 类型 | 字段 | 用途 |
| --- | --- | --- |
| `CacheKey` | cluster_id + req_id (+ prefix_id, model_id) | 按请求 ID 寻址，req_id/prefix_id 二选一有效 |
| `CacheKeyByIdAndIndex` | cluster_id + cache_id + batch_index | 按 cache_id+batch 槽位精确寻址 |
| `BlocksCacheKey` | cluster_id + model_id | PagedAttention 块表寻址 |

#### 4.3.2 核心流程

`CacheManager` 每个方法都遵循统一的三段式：

```text
1. 参数校验：check_isinstance / check_uint32 / raise_if_false（失败抛 LLMException）
2. 类型打包：pack_cache_desc / pack_cache_key 把 Python 对象压成元组
3. 调 wrapper：self._llm_datadist.xxx_v2(...) → handle_llm_status 检查返回码
```

例如一次 `pull_cache`：

```text
pull_cache(CacheKey, cache, batch_index=0, size=-1)
  ├─ 校验 cache_key 类型（开 remote_accessible 时只认 CacheKeyByIdAndIndex）
  ├─ layer_range_to_tensor_indices() 把层区间展开成 tensor 下标
  ├─ pack_cache_key() → 元组
  └─ pull_cache_v2(cache.cache_id, key, param) → C++ 侧 PullCache（u6-l4）
```

#### 4.3.3 源码精读

[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L93-L125](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L93-L125) —— `allocate_cache`：校验 `num_tensors > 0` 且内存池与 placement 匹配（DEVICE 池配 DEVICE placement），把可选的 `cache_keys` 逐个打包后调 `allocate_cache_v2`，返回 `Cache` 对象。docstring 完整说明了引用计数语义：cache_id 引用靠 `deallocate_cache` 解除，cache_keys 引用靠 Decoder `pull_cache` 成功或 Prompt `remove_cache_key` 解除——与 u6-l3 的延迟释放机制对应。

[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L315-L343](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L315-L343) —— `register_cache`：要求 `len(addrs) == num_tensors`、地址非零；关键约束「建链之后不能再注册 remote_accessible 的 Cache」（`_is_call_linked` 检查）；`remote_accessible` 缺省时按 placement 推断——DEVICE 默认 True、HOST 默认 False。

[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L265-L313](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L265-L313) —— `pull_cache`：同步拉取接口。注意第 282-285 行：开启 `remote_cache_accessible` 后只接受 `CacheKeyByIdAndIndex` 寻址；层区间经 `layer_range_to_tensor_indices` 展开后连同 size/batch_index 打进 param 元组调 `pull_cache_v2`。

[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L509-L565](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L509-L565) —— `push_cache`：C2C 直推接口，前置条件 `enable_remote_cache_accessible=True`；`size` 仅支持 -1（全量），层区间缺省展开为全部层，然后**逐层循环**下发 `transfer_cache_v2`——每层一次调用，这是「分层传输」的同步版形态。

[src/python/llm_datadist/llm_datadist/v2/llm_types.py:L93-L156](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py#L93-L156) —— `CacheDesc`：Cache 的形状描述。`batch_size` 取自 `shape[batch_dim_index]`；`size` 属性是惰性计算的——首次访问时调 wrapper 的 `calc_tensor_size` 按 shape 与 DataType 算字节数并缓存。

[src/python/llm_datadist/llm_datadist/v2/llm_types.py:L203-L255](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py#L203-L255) —— `Cache`：用户不应直接构造（由 CacheManager 创建），携带 `cache_id`、`tensor_addrs`、`is_blocks_cache` 等只读属性；另有类方法 `create_cpu_cache` 用于构造 HOST 侧的轻量 Cache 对象（cache_id=-1，不进引擎台账）。

[src/python/llm_datadist/llm_datadist/v2/llm_utils.py:L110-L127](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py#L110-L127) —— pack 函数族：Python 对象 → 定长元组的类型翻译层，例如 `pack_cache_key` 把 `CacheKey` 压成 `(cluster_id, -1, 0, req_id, prefix_id, model_id, False)` 七元组直接喂给 wrapper。这是 Python/C++ 边界上唯一的手工序列化点。

#### 4.3.4 代码实践

1. **实践目标**：通读 `pull_cache_sample.py` 的 Prompt 侧流程，画出「分配→建链→被拉取→释放」的时序。
2. **操作步骤**：
   - 阅读 [examples/python/pull_cache_sample.py:L230-L258](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/pull_cache_sample.py#L230-L258)（`run_prompt_sample`）；
   - 标注每个 `dist.barrier()` 同步点两侧分别完成了什么；
   - 解释第 250-253 行注释：为什么 `remove_cache_key` 在 pull 成功后「只是个空操作」也要调用。
3. **需要观察的现象**：四个 barrier 把流程切成「cache ready → pull 完成 → 双方 unlink 完成」三段；Prompt 侧用 `allocate_cache(cache_desc, [cache_key_0, cache_key_1])` 一次挂两个 CacheKey。
4. **预期结果**：一张时序图 + 文字说明：`remove_cache_key` 是防御性调用——若 Decoder 拉取失败或未拉取，靠它解除 CacheKey 引用，否则 Cache 因引用计数不归零而无法真正释放（对应 u6-l3 的延迟释放机制）。此为源码阅读型实践，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：`pull_cache_sample.py` 里 Prompt 侧为什么不需要 push 任何数据，Decoder 就能拉到？

**答案**：`pull_cache` 是「拉」语义——Decoder 作为发起方，凭 `CacheKey(prompt_cluster_id=1, req_id=...)` 从远端 Prompt 集群单边读取（u6-l4 的 PullKvCache 机制）。Prompt 只需 `allocate_cache` 时挂上 CacheKey 建立索引，传输全程被动。

**练习 2**：`register_cache` 的 `remote_accessible` 参数不传时如何取值？为什么建链后注册会被拒绝？

**答案**：缺省按 placement 推断——DEVICE 为 True、HOST 为 False，且建链后（`_is_call_linked=True`）强制降为 False。因为 remote_accessible 注册要求把内存导出给远端单边访问（u6-l3 的 `remote_accessible` 注册开关），这必须发生在建链协商之前；建链后再变更远端可见性，对端台账无法同步，所以直接拒绝。

### 4.4 异步分层传输：transfer_cache_async 样例精读

#### 4.4.1 概念说明

`transfer_cache_async` 是 Python 侧独有的「异步分层传输」编排接口（对应 u6-l5 的 LayerWise 语义在 Python 层的实现），核心思想：

- 用户实现 `LayerSynchronizer` 抽象基类的 `synchronize_layer(layer_index, timeout)` 回调——它代表「第 N 层的计算完成了」。
- `transfer_cache_async` 启动一条后台线程，**逐层**先等 `synchronize_layer` 返回 True，再把该层发给所有目的地配置——实现「层到即传」的通信/计算重叠。
- 返回 `CacheTask` 句柄，用户随后用 `synchronize()`/`get_results()` 等待结果。

注意：这里的「异步」由 **Python threading.Thread** 实现（不是 C++ 引擎的异步），每层真正下发的仍是同步的 `transfer_cache_v2` 调用。

#### 4.4.2 核心流程

```text
cache_manager.transfer_cache_async(src_cache, layer_synchronizer, [configs])
  ├─ 校验 blocks/cache 组合约束（src 是 blocks 则 dst 必须也是 blocks…）
  ├─ enable_remote_cache_accessible 时 configs 必须全是 TransferWithCacheKeyConfig
  ├─ TransferCacheJob(params, sync, func).init()      # 层数 = num_tensors // 2
  ├─ TransferAsyncThread(job).start()                  # 后台线程
  └─ return CacheTask(thread)

后台线程逐层循环：
  for src_layer_index in range(num_layers):
      sync.synchronize_layer(i)     # 等计算完成
      for config in 命中该层的 configs:
          transfer_cache_v2(...)    # 单边推该层（WRITE）
      全部层完成 → self._rets[dst_cluster_id] = LLM_SUCCESS

用户侧：
  cache_task.synchronize(timeout)   → LLMStatusCode      # 任一失败即返回该失败码
  cache_task.get_results(timeout)   → List[LLMStatusCode]  # 每个目的地一个码
```

#### 4.4.3 源码精读

[src/python/llm_datadist/llm_datadist/v2/cache_manager.py:L490-L507](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/cache_manager.py#L490-L507) —— `transfer_cache_async` 入口：组装 `TransferCacheParameters` 后委托给 `llm_utils.transfer_cache_async`，把 wrapper 的 `transfer_cache_v2` 函数作为回调传入。源 Cache 不允许 HOST placement。

[src/python/llm_datadist/llm_datadist/v2/llm_utils.py:L282-L315](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py#L282-L315) —— 工厂函数本体：先做 blocks/cache 组合校验（源是 blocks 则目的必须也是 blocks 且等长；cache→blocks 必须给 `dst_block_memory_size`），再按 `enable_remote_cache` 决定 configs 只能是 `TransferConfig`（关闭）还是 `TransferWithCacheKeyConfig`（开启）——这正是样例里开启 remote accessible 后用 `TransferWithCacheKeyConfig` 的原因。最后建 job、起线程、包 `CacheTask` 返回。

[src/python/llm_datadist/llm_datadist/v2/llm_utils.py:L168-L192](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py#L168-L192) —— `TransferCacheJob.transfer_layers`：逐层主循环。`to_transfer` 筛出层区间覆盖当前层的配置；`synchronize_layer` 失败则给所有相关目的地记 `LLM_PARAM_INVALID` 并中止；某目的地传完其层区间最后一层即记 `LLM_SUCCESS`。

[src/python/llm_datadist/llm_datadist/v2/llm_utils.py:L194-L217](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py#L194-L217) —— `transfer_layer`：按配置类型把一层翻译成 `transfer_cache_v2` 的十元组——`TransferConfig`（裸地址模式，`PushType.NO_CACHE_KEY`）、`BlocksCacheKey`（`BLOCKS_CACHE_KEY`）或 `CacheKeyByIdAndIndex`（`CACHE_KEY_BY_ID`）三种 PushType 分支。

[src/python/llm_datadist/llm_datadist/v2/llm_utils.py:L249-L273](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_utils.py#L249-L273) —— `TransferAsyncThread`：继承 `Thread`，`get/get_results` 用 `join(timeout)` 实现——超时线程仍存活则返回默认错误码 `LLM_TIMEOUT`。

[src/python/llm_datadist/llm_datadist/v2/llm_types.py:L499-L521](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py#L499-L521) —— `LayerSynchronizer` 抽象基类：只有一个抽象方法 `synchronize_layer(layer_index, timeout_in_millis) -> bool`。样例里的实现恒返回 True（无真实计算可等）。

[src/python/llm_datadist/llm_datadist/v2/llm_types.py:L524-L549](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py#L524-L549) —— `TransferWithCacheKeyConfig`：携带目的端 cache_key（BlocksCacheKey 或 CacheKeyByIdAndIndex）与源/目的层区间，构造时即校验两区间等宽、BlocksCacheKey 时 `src_batch_index` 必须为 0。

[src/python/llm_datadist/llm_datadist/v2/llm_types.py:L669-L685](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/llm_datadist/v2/llm_types.py#L669-L685) —— `CacheTask`：异步句柄，`synchronize` 返回聚合状态码、`get_results` 返回逐目的地状态码列表，超时参数毫秒→秒换算后交给线程 join。

样例侧（Prompt 发起方）：

[examples/python/transfer_cache_async_sample.py:L93-L141](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/transfer_cache_async_sample.py#L93-L141) —— `run_prompt_sample` 四步：① 用 torch_npu 张量的 `data_ptr()` 注册 `register_blocks_cache`；② 构造 `LLMClusterInfo`（本地/远端 ip:port）并发起 `link_clusters`；③ 构造 `TransferWithCacheKeyConfig(BlocksCacheKey(...), range(0,1), range(0,1))` 调 `transfer_cache_async`，随后 `cache_task.get_results()` 等待；④ `unlink_clusters(force=True)` → `unregister_cache` → `finalize` 收尾。

[examples/python/transfer_cache_async_sample.py:L59-L80](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/transfer_cache_async_sample.py#L59-L80) —— `init_llm_datadist`：完整演示了 `LLMConfig` 的关键属性组合——`transfer_backend="hixl"`（可选切到 HIXL 后端）、Decoder/Prompt 各自的 `listen_ip_info`（端口 26000/26001 错开）、`enable_cache_manager=True`、`enable_remote_cache_accessible=True`。

[examples/python/transfer_cache_async_sample.py:L83-L90](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/transfer_cache_async_sample.py#L83-L90) —— `LayerSynchronizerImpl`：样例对 `LayerSynchronizer` 的最小实现，`synchronize_layer` 恒返回注入的布尔值——把它改成 `False` 可以观察传输在第 0 层即失败的行为（见练习）。

#### 4.4.4 代码实践

1. **实践目标**：在两台互通的昇腾机器（或单机双卡）上分别运行两个样例，对比「同步 Pull」与「异步分层传输」的接口差异并记录输出。
2. **操作步骤**：
   - 环境准备：加载 CANN 环境，构建并 `pip install` llm_datadist whl，安装 torch 与 torch_npu；
   - 样例一（同步 Pull，两机各起一进程）：

     ```bash
     # Prompt 机（cluster_id=1）
     python pull_cache_sample.py --device_id 0 --cluster_id 1 --host_ip <prompt_host_ip>
     # Decoder 机（cluster_id=2）
     python pull_cache_sample.py --device_id 0 --cluster_id 2 --host_ip <prompt_host_ip>
     ```

     单机双卡可加 `--is_single 1`（rank table 会自动用 `hccn_tool` 探测本机 device IP）；
   - 样例二（异步分层传输，注意参数是 role 而非 cluster_id）：

     ```bash
     # Prompt 侧
     python transfer_cache_async_sample.py --role p --device_id 0 \
         --local_host_ip <本机IP> --remote_host_ip <对端IP> [--transfer_backend hixl]
     # Decoder 侧
     python transfer_cache_async_sample.py --role d --device_id 0 \
         --local_host_ip <本机IP> --remote_host_ip <对端IP>
     ```

   - 记录两侧日志：建链成功、`register/allocate` 的 cache_id、传输完成、Decoder 侧打印的 tensor 内容。
3. **需要观察的现象**：
   - 样例一 Decoder 侧应看到 `[allocate_cache] success`、`[pull_cache] success`；Prompt 侧 `[remove_cache_key] success`；
   - 样例二 Decoder 侧第 165-166 行会把收到的 tensor 搬回 CPU 打印——数值应全为 1.0（Prompt 用 `torch.ones` 填充，传输层范围 `range(0,1)` 即第 0 层的两个 tensor）；
   - 加 `--transfer_backend hixl` 重跑样例二，观察建链与传输是否同样成功（后端切换对接口透明）。
4. **预期结果**：整理出一张接口对照表（见下方「综合实践」的表格模板）。本环境无昇腾硬件，**运行结果待本地验证**；无硬件时可退化为源码阅读实践——静态对比 `run_decoder_sample`（pull_cache_sample.py:L201-L227）与 `run_prompt_sample`（transfer_cache_async_sample.py:L93-L141）的调用序列完成同一张表。

#### 4.4.5 小练习与答案

**练习 1**：把 `LayerSynchronizerImpl(True)` 改成 `LayerSynchronizerImpl(False)` 会发生什么？

**答案**：`transfer_layers` 第 174-179 行：`synchronize_layer` 返回 False 时，该层所有目的地被记 `LLMStatusCode.LLM_PARAM_INVALID` 并立即 return，线程结束；随后 `cache_task.get_results()` 返回 `[LLM_PARAM_INVALID]`，一行数据都不会传输。真实业务里这个回调应该挂在「第 N 层前向计算完成」的事件上，从而实现层粒度的通信/计算重叠。

**练习 2**：`CacheTask.synchronize()` 和 `get_results()` 返回值有何区别？超时如何表现？

**答案**：`synchronize()` 聚合——所有目的地都成功返回 `LLM_SUCCESS`，任一失败返回第一个非成功码；`get_results()` 细化——按配置顺序返回每个目的地的状态码列表。两者都接受毫秒级 timeout，实现是 `join(timeout)`：超时后线程仍存活时不抛异常，而是返回默认错误码 `LLM_TIMEOUT`（`get_results` 返回全 `LLM_TIMEOUT` 列表）。

**练习 3**：为什么样例二的 `TransferWithCacheKeyConfig` 层区间是 `range(0, 1)` 而不是全部层？

**答案**：样例 Cache 的 `num_tensors=2`，按「一层 2 个 tensor」约定共 1 层（第 0 层），所以 `range(0,1)` 已经是全部层。层区间机制的价值在真实模型（如几十层 KV）中体现：可以为不同目的地配置不同的层区间，实现分层、分目的地的灵活分发。

## 5. 综合实践

**任务：制作「同步 Pull vs 异步分层传输」接口对照表并跑通两条路径。**

在完成 4.4.4 实践（或其源码阅读退化版本）的基础上，填写下面表格并撰写一段分析：

| 维度 | pull_cache_sample.py | transfer_cache_async_sample.py |
| --- | --- | --- |
| 发起方角色 | Decoder（拉） | Prompt（推） |
| 初始化关键选项 | `enable_cache_manager`、`mem_pool_cfg` | 另需 `enable_remote_cache_accessible`、`listen_ip_info`，可选 `transfer_backend` |
| 获得 Cache 的方式 | `allocate_cache`（内存池） | `register_blocks_cache`（torch_npu data_ptr 注册） |
| 寻址类型 | `CacheKey(req_id)` | `BlocksCacheKey` + `TransferWithCacheKeyConfig` |
| 建链接口 | `link` + `query_register_mem_status` 轮询（旧式 rank table） | `link_clusters`（ip:port 交换，返回状态码） |
| 传输接口 | `pull_cache`（同步阻塞） | `transfer_cache_async` + `CacheTask.get_results`（Python 线程逐层推） |
| 层粒度控制 | kwargs 里的 `src_layer_range`/`dst_layer_range` | 配置对象里的层区间 + `LayerSynchronizer` 回调 |
| 进程间同步 | `dist.barrier()` ×4 | `dist.barrier()` ×2 |
| 收尾顺序 | unlink → deallocate → finalize | unlink → unregister → finalize |

进一步（可选）：给样例二加上 `--transfer_backend hixl` 重跑，结合 u6-l7 的 `HixlTransferEngine` 适配层知识，解释为什么切换后端不需要改任何一行业务代码（提示：Python 层只透传 `llm.TransferBackend` 选项，适配发生在 C++ TransferEngineFactory）。

## 6. 本讲小结

- `llm_datadist` Python 包是「纯 Python 外壳 + wrapper .so」的混合包，经 `setup.py bdist_wheel` 打成 whl 发布；`__init__.py` 导出约 20 个公开符号，v1 遗留类走 `__getattr__` 懒加载。
- `LLMDataDist.init` 按是否启用 CacheManager 双路由到 v2/v1 wrapper；全进程单例；`atexit` 兜底 finalize。`LLMConfig` 用属性风格生成选项字典，`llm.LocalCommRes`/`llm.TransferBackend` 有联动默认值与强校验。
- `CacheManager` 的每个方法都是「参数校验 → pack 元组 → 调 wrapper → 状态码转异常」四段式；`CacheKey`（按请求）、`CacheKeyByIdAndIndex`（按槽位）、`BlocksCacheKey`（按块表）三类寻址对应 u6 系列讲义的 CacheIndex 机制。
- `pull_cache` 是同步拉取（Decoder 发起）；`push_cache`/`push_blocks`/`transfer_cache_async` 要求开启 `enable_remote_cache_accessible`，走 C2C 直推。
- `transfer_cache_async` 的「异步」由 Python `threading.Thread` 实现：逐层先等用户提供的 `LayerSynchronizer.synchronize_layer` 回调，再逐目的地下发同步的 `transfer_cache_v2`，实现「层到即传」的通信/计算重叠；`CacheTask.synchronize/get_results` 聚合/细化返回结果，超时返回 `LLM_TIMEOUT`。
- 建链之后不能再注册 remote_accessible 的 Cache；Prompt 侧 `remove_cache_key` 是保证 CacheKey 引用计数收敛的防御性调用。

## 7. 下一步学习建议

- **下一讲 u7-l3（PD 分离端到端）**：把本讲两个样例的接口串成完整的 Prompt/Decoder 双进程业务流，并对比 C++ 样例（`prompt_push_cache_and_blocks.cpp`）的写法差异。
- **继续阅读源码**：`src/python/llm_datadist/llm_datadist/utils/utils.py`（全部 check_* 校验函数的实现）；`examples/python/` 下其余样例（`push_blocks_sample.py`、`pull_from_cache_to_blocks.py`、`switch_role_sample.py`）覆盖了 blocks 混合传输与角色切换。
- **回溯 C++ 层**：带着本讲的 `transfer_cache_v2` 十元组去读 `src/llm_datadist/api/llm_datadist_impl.cc` 中 Push/Pull 的实现（u6-l4），理解 Python 元组每个字段在 C++ 侧落到哪个结构体。
- **查阅文档**：[docs/zh/api/python/CacheManager.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/python/CacheManager.md) 与 [docs/zh/api/python/LLMConfig.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/python/LLMConfig.md) 是日常开发最常翻的两页。
