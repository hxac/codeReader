# u7-l1 HIXL Python 绑定

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `src/python/hixl_py/` 中 pybind11 绑定代码的三层组织方式（C++ 包装类 → 注册函数 → `PYBIND11_MODULE` 入口）。
2. 列出 Python 侧 `hixl` 模块暴露的全部类、方法、常量，并能把它们与 `hixl::Hixl` C++ 公开 API 一一对应。
3. 理解包装层为适配 Python 所做的关键类型翻译（`AscendString`→`std::string`、`void*` 句柄→`uintptr_t`、出参→`pair` 返回值）与 GIL（全局解释器锁）处理。
4. 掌握 `hixl.so` 的 CMake 构建方式、安装脚本的部署逻辑，以及与之配套的 `llm_datadist` whl 打包流水线。

## 2. 前置知识

在阅读本讲前，你需要了解以下概念（均在前序讲义中出现过，这里做简要回顾）：

- **pybind11**：一个仅头文件的 C++ 库，用于把 C++ 类、函数、枚举映射成 Python 的类、函数、枚举，最终产出一个可供 `import` 的 `.so` 扩展模块。它需要绑定代码「逐个注册」要暴露给 Python 的实体。
- **GIL（Global Interpreter Lock，全局解释器锁）**：CPython 的同一时刻只有一个线程执行 Python 字节码的机制。C++ 代码在持有 GIL 时若执行长耗时操作（如同步传输），会卡死其他 Python 线程；pybind11 提供 `py::gil_scoped_release` 在进入 C++ 重活前临时放锁。
- **句柄（handle）的不透明性**：u2-l2 讲过，`MemHandle` 与 `TransferReq` 在 C++ 侧是 `void*`，用户只持有、不解引用。Python 没有指针类型，绑定层把它们翻译成整数 `uintptr_t`。
- **`AscendString`**：CANN ge_common 提供的字符串类，`Hixl` C++ API 用它接收 engine 标识与选项；Python 侧只有 `str`，绑定层负责双向转换。
- **HIXL 调用序列**（u1-l3、u2-l5）：Initialize → RegisterMem → 地址交换 → Connect → TransferSync/Async → Disconnect → Finalize。本讲的样例就是这个序列的 Python 版。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/python/hixl_py/hixl_py.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.h) | `HixlPy` C++ 包装类声明：持有 `hixl::Hixl` 实例，把公开 API 翻译成 Python 友好签名 |
| [src/python/hixl_py/hixl_py.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc) | 包装类实现 + 四个 `Register*` 函数 + `PYBIND11_MODULE` 模块入口 |
| [src/python/hixl_py/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/CMakeLists.txt) | 把 `hixl_py.cc` 编成 `hixl.so` 模块并安装 |
| [src/python/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/CMakeLists.txt) | Python 目录总入口，挂接 llm_datadist / llm_wrapper / metadef_wrapper / hixl_py 四个子目录 |
| [src/python/llm_datadist/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/CMakeLists.txt) | 对照参考：`llm_datadist` whl 的打包流水线（setup.py bdist_wheel） |
| [scripts/package/hixl/scripts/hixl_custom_install.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh) | 安装脚本：把 `llm_datadist` whl 与 `hixl.so` 部署到 site-packages |
| [examples/python/hixl_d2rd_multiproc_sample.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py) | 本讲实践样例：torch_npu + hixl 双进程 D2rD READ 传输 |

背景说明：本讲涉及的绑定与打包能力由仓库 HEAD 提交 `a5dd1de [feat]: Add HIXL Engine Python binding and whl packaging` 引入，属于较新的代码。

## 4. 核心概念与源码讲解

### 4.1 HixlPy 包装类：类型翻译与线程安全

#### 4.1.1 概念说明

pybind11 可以直接绑定 `hixl::Hixl`，但 `Hixl` 的 C++ 签名对 Python 并不友好：

- 参数用 `AscendString` 而非 `std::string`；
- 句柄是 `void*`，Python 无法表达；
- 结果常通过出参（如 `hixl::Status RegisterMem(..., hixl::MemHandle &handle)`）返回，而 Python 习惯用返回值。

所以绑定层先写一个 **C++ 包装类 `HixlPy`**，把「类型翻译」集中在这一层；pybind11 注册层（4.2）只面对干净签名。这是 pybind11 项目的常见分层：**包装类负责语义适配，注册代码只负责暴露**。

#### 4.1.2 核心流程

`HixlPy` 对每个接口做四件事：

1. 加互斥锁（`std::mutex`），使同一实例可被多线程 Python 调用而不会并发进入引擎；
2. 检查 `initialized_` 门卫，未初始化直接返回 `hixl::FAILED`（对应 C++ 侧的 impl 门卫检查）；
3. 做类型翻译（`std::string`→`AscendString`、`uintptr_t`→`void*`）；
4. 把出参打包成 `std::pair<Status, 值>` 返回，Python 侧解包成元组。

#### 4.1.3 源码精读

`HixlPy` 的成员与签名总览——注意与 [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) 对照，每个方法都对应 `Hixl` 的一个公开接口：

[HixlPy 类声明](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.h#L23-L54)：声明了生命周期、内存、链路、传输、通知五组方法，私有成员持有引擎实例与状态（`hixl_engine_`、`mutex_`、`initialized_`）。

以 `RegisterMem` 为例看出参→pair 的翻译——C++ 侧 `MemHandle`（`void*`）出参被改写成 `(Status, uintptr_t)` 返回对：

- [RegisterMem 返回 pair](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L70-L78)：`reinterpret_cast<uintptr_t>(handle)` 把指针变成整数，Python 侧拿到的 handle 是一个 `int`。
- [DeregisterMem 反向翻译](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L80-L87)：`reinterpret_cast<hixl::MemHandle>(mem_handle)` 把整数还原成指针。两个方向合起来，Python 用户就可以把 handle 当普通整数保存和传递。

字符串翻译以 `Connect` 为例：

- [Connect 的 AscendString 转换](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L89-L96)：`std::string` 先构造 `hixl::AscendString`，再调用 C++ `Connect`。`Initialize` 的 options 也一样——Python 的 `dict[str, str]`（绑定到 `std::map<std::string, std::string>`）逐项转成 `map<AscendString, AscendString>`（见 [hixl_py.cc:38-58](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L38-L58)），且重复 Initialize 幂等（记日志直接返回 SUCCESS）。

两个值得注意的细节：

- **析构函数主动放 GIL**：[HixlPy::~HixlPy](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L29-L36) 中 `py::gil_scoped_release release;` 后调用 `Finalize()`。若 Python 对象被 GC 回收时忘记 `finalize()`，析构会代为收尾，而 Finalize 可能耗时（断链、释放资源），放锁避免阻塞其他线程。
- **`GetAllAsyncConnectStatus` 的 map 回译**：[hixl_py.cc:136-148](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L136-L148) 把 `map<AscendString, AsyncConnectStatus>` 逐项回译成 `map<std::string, ...>`，配合 pybind11 的 STL 头自动变成 Python `dict`。

#### 4.1.4 代码实践

**实践目标**：手工完成一次「C++ 签名 → HixlPy 签名 → Python 签名」的三级翻译，检验对类型翻译规则的理解。

**操作步骤**：

1. 打开 [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h)，找到 `TransferAsync`、`GetTransferStatus`、`GetNotifies` 三个声明。
2. 对照 [hixl_py.h:39-47](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.h#L39-L47) 与 [hixl_py.cc:160-193](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L160-L193)，抄下三级签名。
3. 写一张三列对照表（C++ / HixlPy / 预期 Python），标注每处翻译属于哪种规则（AscendString→string、void*→uintptr_t、出参→pair、vector→list）。

**需要观察的现象**：`GetTransferStatus` 的 C++ 出参有两个（`Status` 与 `TransferStatus &`），HixlPy 把它折成 `pair<Status, TransferStatus>`——Python 侧一次调用返回二元组。

**预期结果**：得到一张约 5 行的翻译规则表；结论应与 4.2 节 Python 侧实际暴露一致。本实践为源码阅读型，无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HixlPy` 每个方法都要检查 `initialized_`，而 C++ `Hixl` 类不用（它检查什么）？

**答案**：C++ `Hixl` 检查的是 Pimpl 的 `impl_` 是否为空（u2-l1 的两道门卫）。Python 侧 GC 时序不可控、用户可能先 `finalize()` 再调方法，`HixlPy` 用自己的 `initialized_` 布尔快速短路，未初始化统一返回 `hixl::FAILED`，避免对空 `unique_ptr` 解引用。

**练习 2**：把 `MemHandle` 暴露成 Python `int` 而不是封装一个 Handle 类，有什么得与失？

**答案**：得——实现简单、可直接存入 Python 容器、跨 `ctypes`/torch 生态传递无障碍；失——失去类型安全，任何整数传给 `deregister_mem` 都能编译/运行，错误只在引擎侧校验时暴露（返回错误码），且 Python 侧无法区分 handle 与普通整数。

**练习 3**：`HixlPy::Initialize` 在已初始化时返回 SUCCESS 而不是报错，这与 C++ 侧行为一致吗？

**答案**：方向一致但层次不同。C++ `HixlImpl::Initialize` 幂等（重复调用直接成功，u2-l1）；`HixlPy` 在包装层就拦截了重复调用并记 `HIXL_LOGI` 日志，不会到达引擎。两者都不把重复 Initialize 视为错误。

### 4.2 pybind11 模块注册：Python 视角的 API 全景

#### 4.2.1 概念说明

模块入口 `PYBIND11_MODULE(hixl, m)` 定义了 Python 里 `import hixl` 得到的东西。本项目的注册代码按「四类实体」分函数组织，每类一个 `Register*` 函数：

| 注册函数 | 暴露内容 | Python 侧形态 |
| --- | --- | --- |
| `RegisterConstants` | 9 个错误码 + 7 个选项键 + 2 个能力值 | 模块级常量 `hixl.SUCCESS`、`hixl.OPTION_AUTO_CONNECT` 等 |
| `RegisterEnums` | MemType、TransferOp、TransferStatus、AsyncConnectStatus、FeatureType | 枚举类 `hixl.MemType.MEM_DEVICE` 等 |
| `RegisterDataClasses` | MemDesc、TransferOpDesc、TransferArgs、GetTransferStatusArgs、TransferResult、NotifyDesc | 可构造数据类，字段可读写 |
| `RegisterHixlEngine` | `HixlPy` 包装类与静态 `get_capability` | `hixl.Hixl` 类（snake_case 方法） |

这种「按实体类型分函数」的写法让注册代码可维护——新增一个枚举只改 `RegisterEnums`，不碰其他部分。

#### 4.2.2 核心流程

模块导入时的执行流程：

```text
import hixl
  └─ dlopen hixl.so，CPython 调 PyInit_hixl（由 PYBIND11_MODULE 生成）
       ├─ RegisterConstants(m)   ← 错误码/选项键变成模块属性
       ├─ RegisterEnums(m)       ← py::enum_ 生成枚举类型
       ├─ RegisterDataClasses(m) ← py::class_ 生成数据类
       └─ RegisterHixlEngine(m)  ← py::class_<HixlPy> 生成 Hixl 类
```

#### 4.2.3 源码精读

[PYBIND11_MODULE 入口](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L354-L359)：`PYBIND11_MODULE(hixl, m)` 的第一个参数 `hixl` 决定模块名与 `import hixl` 的行为，宏内部生成 `PyInit_hixl`，再顺序调用四个注册函数。

[RegisterConstants](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L220-L241)：把 C++ `constexpr` 错误码（`hixl::SUCCESS` 等 9 个，u2-l2 讲过）与选项键字符串（`OPTION_ENABLE_USE_FABRIC_MEM` 等 7 个）逐个 `m.attr(...)` 成模块属性。注意错误码是 `py::int_`、选项键是 `py::str`——Python 用户写 `hixl.OPTION_AUTO_CONNECT` 拿到的是字符串 `"auto_connect"`，可直接作 options 字典的 key。

[RegisterEnums](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L243-L271)：`py::enum_` 注册五个枚举。`MemType`、`TransferOp`、`AsyncConnectStatus` 加了 `export_values()`（枚举值同时注入模块命名空间），而 `TransferStatus` 没有——后者必须写全 `hixl.TransferStatus.COMPLETED`。`FeatureType` 暴露了 `AUTO_CONNECT` 与 `CLIENT_SERVER_COMM` 两个特性查询项（对应 u2-l2 的 `GetCapability`）。

[RegisterDataClasses](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L273-L320)：六个数据类。两个关键写法：

- `MemDesc` / `TransferOpDesc` 用带名参数构造器（`py::arg("addr")`），Python 侧可以 `hixl.MemDesc(dev_addr, BUF_SIZE)` 或关键字传参（见 4.4 样例）。
- 含 `void*` 字段的类（`TransferArgs.user_data`、`TransferResult.req`）不能直接暴露，改用 `def_property` 加 lambda 在 `uintptr_t` 与指针间 `reinterpret_cast`——与 4.1 的句柄翻译同一套手法。
- `NotifyDesc` 的两个 `AscendString` 字段同样用 `def_property` 双向转 `std::string`。

[RegisterHixlEngine](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L322-L352)：注册 `HixlPy` 为 Python 类 `hixl.Hixl`，17 个方法全部 snake_case，并给超时参数绑定默认值（`timeout_in_millis = 1000`）。**每个方法都挂了 `py::call_guard<py::gil_scoped_release>()`**——进入 C++ 实现前自动放掉 GIL，函数返回后自动收回。这保证了同步传输（可能阻塞数秒）期间其他 Python 线程仍可运行。静态能力查询注册为模块级函数 [get_capability](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L351)，与 C++ 静态 `Hixl::GetCapability` 对应。

由此得到 Python 侧接口全景（方法名 → C++ 方法）：

| Python（`hixl.Hixl` 实例方法） | C++（`HixlPy`/`Hixl`） | 返回 |
| --- | --- | --- |
| `initialize(local_engine, options={})` | `Initialize` | `int`（Status） |
| `finalize()` | `Finalize` | None |
| `register_mem(mem_desc, mem_type)` | `RegisterMem` | `(int, int)` Status+handle |
| `deregister_mem(mem_handle)` | `DeregisterMem` | `int` |
| `connect / disconnect / connect_async / disconnect_async` | 同名 C++ | `int` |
| `get_async_connect_status(remote_engine)` | `GetAsyncConnectStatus` | `(int, AsyncConnectStatus)` |
| `get_all_async_connect_status()` | `GetAllAsyncConnectStatus` | `(int, dict[str, AsyncConnectStatus])` |
| `transfer_sync(remote_engine, op, op_descs, timeout_in_millis=1000)` | `TransferSync` | `int` |
| `transfer_async(remote_engine, op, op_descs, args=TransferArgs())` | `TransferAsync` | `(int, int)` Status+req_id |
| `get_transfer_status(req_id)` | `GetTransferStatus` | `(int, TransferStatus)` |
| `get_all_transfer_status(args=...)` | `GetAllTransferStatus` | `(int, list[TransferResult])` |
| `send_notify(remote_engine, notify, timeout_in_millis=1000)` | `SendNotify` | `int` |
| `get_notifies()` | `GetNotifies` | `(int, list[NotifyDesc])` |
| 模块函数 `get_capability(feature_type)` | 静态 `GetCapability` | `(int, int)` |

#### 4.2.4 代码实践

**实践目标**：不写一行 C++，列出 `hixl` 模块在 Python 侧的完整公开面。

**操作步骤**：

1. 按 u1-l2 构建（`bash build.sh --examples`），在安装后确认 `hixl.so` 可导入。
2. 执行（示例代码，非项目原有）：

   ```python
   import hixl
   print([n for n in dir(hixl) if not n.startswith("_")])
   ```

3. 对每个类继续下钻，如 `dir(hixl.Hixl)`、`dir(hixl.TransferOpDesc)`；再用 `help(hixl.Hixl.transfer_sync)` 查看签名与默认参数。
4. 把输出与 4.2.3 的表格逐行核对，标记任何不一致处。

**需要观察的现象**：`dir(hixl)` 中应出现 `Hixl`、`MemDesc`、`TransferOpDesc`、`TransferArgs`、`GetTransferStatusArgs`、`TransferResult`、`NotifyDesc`、五个枚举、`get_capability`，以及 `SUCCESS`、`OPTION_AUTO_CONNECT` 等常量。

**预期结果**：得到一张与 4.2.3 表格吻合的清单。若当前环境无 CANN/昇腾硬件无法构建，本步骤**待本地验证**；替代做法是纯源码阅读：以 [hixl_py.cc:220-359](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L220-L359) 为唯一事实来源手工推导该清单。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TransferStatus` 枚举没有 `export_values()`，而 `MemType` 有？如果两个枚举都有名为 `FAILED` 的值且都导出，会发生什么？

**答案**：`export_values()` 把枚举值注入模块命名空间，方便少打字，但会共享命名空间。`TransferStatus.FAILED` 与错误码 `hixl.FAILED` 同名，若导出会冲突覆盖（后注册者生效），所以只在无冲突的枚举上使用导出；`TransferStatus` 必须带类名前缀访问。

**练习 2**：`transfer_sync` 的超时参数在 Python 里叫 `timeout_in_millis`，而 C++ 里 `HixlPy::TransferSync` 叫 `timeout_ms`。这个名字是在哪里定下来的？

**答案**：在 [hixl_py.cc:340-341](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L340-L341) 的 `py::arg("timeout_in_millis") = 1000`。pybind11 的 `py::arg` 既命名关键字参数又给默认值，是 Python 侧签名的唯一决定点。

**练习 3**：`py::call_guard<py::gil_scoped_release>()` 若被误删，程序功能还正确吗？

**答案**：单线程下功能仍正确（GIL 只是锁，不是正确性来源）；多线程下其他 Python 线程会在整个同步传输期间被阻塞，性能急剧退化。更隐蔽的风险是若 C++ 内部回调进入 Python 而未持锁会崩溃，但本绑定的调用路径都是「单向进入 C++」，所以主要影响是并发性能而非正确性。

### 4.3 构建与打包：从 hixl_py.cc 到 hixl.so 与 whl

#### 4.3.1 概念说明

`src/python/` 下有两类 Python 产物，打包方式不同，容易混淆：

- **`hixl.so`**（本讲主角）：pybind11 直接编译出的**裸扩展模块**，不含 Python 源文件，安装脚本直接复制到 site-packages。
- **`llm_datadist-0.0.1-py3-none-any.whl`**：先由 CMake 调 `setup.py bdist_wheel` 打出的 wheel 包（内含 Python 源码包 `llm_datadist/` 及其两个 `.so` 依赖），安装时经 pip 安装。

两者都在 [src/python/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/CMakeLists.txt#L11-L14) 的四个子目录中挂接。

#### 4.3.2 核心流程

```text
build.sh（u1-l2）
  └─ 顶层 CMake → add_subdirectory(src) → add_subdirectory(src/python)
       ├─ hixl_py/     → add_library(hixl MODULE hixl_py.cc) → hixl.so → install 到 lib/
       └─ llm_datadist/ → 自定义命令跑 setup.py bdist_wheel → llm_datadist-*.whl → install 到 lib/

软件包安装（hixl_custom_install.sh）
  ├─ pip3 安装 llm_datadist-*.whl
  ├─ cp hixl.so → site-packages/
  └─ mkdir site-packages/hixl; ln -sf ../hixl.so site-packages/hixl/hixl.so
```

#### 4.3.3 源码精读

[hixl_py/CMakeLists.txt:11-14](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/CMakeLists.txt#L11-L14)：整个子目录包在 `if (NOT ENABLE_TEST)` 里——跑测试（u1-l2 的 build_test 桩环境）时不编译绑定；正常构建时用 `add_library(hixl MODULE ...)` 产生可加载模块而非普通静态/动态库。

[目标属性与链接](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/CMakeLists.txt#L16-L49)：头文件路径包含 Python 头（`HI_PYTHON_INC`）与 pybind11（`pybind11_INCLUDE_DIR`，均由上层 CMake 探测）；链接 `cann_hixl`（引擎本体）与 `alog`、`intf_pub` 等；`PREFIX ""` + `SUFFIX ".so"` 保证产物文件名恰好是 `hixl.so`（CPython 扩展模块要求 `模块名.so`）；`-Xlinker -export-dynamic` 与 `-s`（strip）分别保证符号可见性与体积；`PYBIND11_NO_ASSERT_GIL_HELD_INCREF_DECREF` 宏关掉 pybind11 的 GIL 断言，配合各方法自己管理放锁。

[install 规则](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/CMakeLists.txt#L51-L54)：`hixl.so` 安装到 `INSTALL_LIBRARY_DIR`（软件包的 lib 目录），进入软件包分发链路。

对照参考——whl 是怎么打出来的（llm_datadist 的做法）：[src/python/llm_datadist/CMakeLists.txt:11-26](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/llm_datadist/CMakeLists.txt#L11-L26) 用 `add_custom_command` 拷贝 `setup.py`/`MANIFEST.in`/Python 包源码及两个 wrapper `.so` 到临时目录 `wheel1/`，再执行 `${HI_PYTHON} setup.py bdist_wheel`，最后把 `dist/` 下的 whl 拷回并安装。**依赖声明** `DEPENDS version_hixl_info llm_datadist_wrapper metadef_wrapper` 保证 wrapper so 先编好——`hixl.so` 没有走这条路，因为它是单文件模块，无需打入包结构。

安装脚本如何部署两者：[hixl_custom_install.sh:325-327](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh#L325-L327) 定义了三个路径——whl 安装目录 `python/site-packages`、llm_datadist 的 whl、以及 `hixl.so`。

- [pip 安装 whl](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh#L101-L115)：`hixl_install_package` 按 `--pylocal` 参数决定用 `pip3 install -t`（装到 CANN 路径）还是 `--user`（装到用户 Python 路径），均带 `--no-deps`。
- [部署 hixl.so](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh#L346-L364)：先 `cp -f` 把 `hixl.so` 复制到 site-packages 根，再创建 `hixl/` 子目录并 `ln -sf ../hixl.so` 建软链接。日志同时提示：不选 `--pylocal` 时 llm_datadist 装到 Python 默认路径、而 `hixl.so` 在 CANN 的 site-packages，需要保证 `PYTHONPATH` 覆盖。

#### 4.3.4 代码实践

**实践目标**：搞清楚「我 `import hixl` 时加载的到底是哪个文件」，并验证 whl 与 so 的部署差异。

**操作步骤**：

1. 构建后在产物目录查找两个文件（示例命令）：

   ```bash
   find build_out -name "hixl.so" -o -name "llm_datadist-*.whl"
   ```

2. 若已安装软件包，在 Python 里执行 `import hixl; print(hixl.__file__)`，确认加载路径是 site-packages 下的 `hixl.so`。
3. 对照 [hixl_custom_install.sh:347-357](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh#L347-L357)，用 `ls -l` 观察 site-packages 根的 `hixl.so` 与 `hixl/hixl.so` 软链接的指向关系。
4. 检查 `pip3 show llm_datadist` 与 `python3 -c "import llm_datadist; print(llm_datadist.__file__)"` 的位置差异。

**需要观察的现象**：`hixl` 是单文件扩展模块（`__file__` 以 `hixl.so` 结尾），`llm_datadist` 是包目录（`__init__.py` 所在目录）；`hixl/hixl.so` 是指向 `../hixl.so` 的符号链接。

**预期结果**：能画出「lib/hixl.so 与 lib/*.whl → site-packages」的部署图。无安装环境时**待本地验证**；源码阅读型替代：只执行步骤 1 的 `find`，再从 install 脚本推导步骤 3 的链接关系。

#### 4.3.5 小练习与答案

**练习 1**：`add_library(hixl MODULE ...)` 与普通 `SHARED` 庩有什么区别？为什么这里必须用 MODULE？

**答案**：MODULE 生成「运行时 dlopen 加载」的库，不参与 `target_link_libraries` 链接；SHARED 库是给链接期用的 `-lhixl`。CPython 扩展模块只被解释器 `dlopen`，没人链接它，且命名必须精确等于 `hixl.so`（配合 `PREFIX ""`），用 MODULE 语义更准确也避免被误链接。

**练习 2**：为什么 `hixl.so` 不像 llm_datadist 一样打进 whl？

**答案**：whl 打包需要包结构（setup.py + Python 源码 + MANIFEST），适合「Python 源码 + 附属 so」的混合包；`hixl` 是纯 C++ 单文件模块，没有 Python 源码，直接复制文件即可，走 whl 反而增加流程。安装脚本用 cp + 软链接达成同样效果（[hixl_custom_install.sh:347-353](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/scripts/package/hixl/scripts/hixl_custom_install.sh#L347-L353)）。

**练习 3**：构建日志若显示 `ENABLE_TEST` 为真，`hixl.so` 还会被编译吗？

**答案**：不会。[hixl_py/CMakeLists.txt:11](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/CMakeLists.txt#L11) 的整个目标定义包在 `if (NOT ENABLE_TEST)` 中——测试构建（build_test 目录、桩环境）只编译测试所需目标。这也是排查「为什么找不到 hixl.so」时的第一检查点：确认不是用 run_test.sh 的构建产物。

### 4.4 实战样例：hixl_d2rd_multiproc_sample.py 精读

#### 4.4.1 概念说明

这个样例是 C++ 版 `hixl_example_d2rd_multiproc`（u1-l5）的 Python 翻版：**两个进程**（server/client）、**两条通道**——TCP socket 控制面交换地址 + HIXL 数据面 READ 传输。它展示了 Python 绑定的典型用法：用 torch_npu 分配 NPU 内存，把 `data_ptr()` 整数直接交给 `hixl` 注册与传输——这正是句柄/地址翻译成 `int` 的红利。

#### 4.4.2 核心流程

```text
server 进程                          client 进程
────────────                         ────────────
set_device(2)                        set_device(0)
Hixl().initialize("127.0.0.1:16001") Hixl().initialize("127.0.0.1:16000")
torch.full(8MB, 0xAA) on npu         torch.zeros(8MB) on npu
register_mem(MemDesc(addr, 8MB))     register_mem(MemDesc(addr, 8MB))
TCP(:16001+1000) 发送 8 字节地址  →   TCP 收 8 字节 → remote_addr
（等 client 断开）                    connect("127.0.0.1:16001")
                                      transfer_sync(READ, 512×16KB descs)
                                      校验全部字节 == 0xAA
                                      disconnect → deregister_mem → finalize
deregister_mem → finalize
```

512 条 `TransferOpDesc` 批量下发（`BLOCK_COUNT = BUF_SIZE // BLOCK_SIZE`），与 u2-l5 讲的「批量是第一公民」呼应。

#### 4.4.3 源码精读

[样例常量区](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L27-L42)：默认 client 用 device 0、server 用 device 2（兼容 A3 单卡双 die 不互通，u1-l3 讲过）；`SOCKET_PORT_OFFSET = 1000`——控制面端口 = 引擎端口 + 1000，与 C++ 样例同款约定。

[run_server](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L162-L196)：server 侧只做被动方三件事——initialize、`torch.full` 填 0xAA 并取 `int(buf_tensor.data_ptr())` 得设备地址、`register_mem`；随后经 TCP 把 8 字节地址发给 client（[server_send_addr](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L106-L125) 用 `struct.pack("!Q", dev_addr)` 打包大端无符号 64 位）。`finally` 块保证 deregister + finalize。

[run_client](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L243-L291)：client 是主动方。注意 Python 绑定 API 的实际形态——`engine.connect(args.remote_engine, timeout_in_millis=CONNECT_TIMEOUT_MS)`、`engine.transfer_sync(args.remote_engine, hixl.TransferOp.READ, op_descs, timeout_in_millis=TRANSFER_TIMEOUT_MS)`，返回值是裸 Status 整数，逐次与 `hixl.SUCCESS` 比较。

[批量描述符构造](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L199-L211)：循环生成 512 个 `hixl.TransferOpDesc(local_addr=..., remote_addr=..., len=16KB)`，本地/远端地址按块偏移对齐——Python 列表经 pybind11 的 STL 转换变成 `std::vector<hixl::TransferOpDesc>`。

[数据校验](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L214-L220)：`buf_tensor.cpu().numpy()` 拷回主机后与 `0xAA` 填充的期望逐字节比对——完成感知靠本次 `cpu()` 拷贝的隐式同步（device 内传输完成性由 `transfer_sync` 返回 SUCCESS 保证）。

[角色与默认参数解析](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L294-L321)：`--role` 必填（client/server 二选一），device/engine 地址按角色给默认值；[parse_engine_addr](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L45-L66) 同时兼容 ipv4 `host:port` 与 ipv6 `[host]:port`，与 u2-l1 讲的引擎标识格式一致。

#### 4.4.4 代码实践

**实践目标**：在真实昇腾环境跑通 Python 版 D2rD 传输，验证 `hixl.so` 绑定可用，并体会 Python 侧 API 与 C++ 样例（u1-l5）的一一对应。

**操作步骤**：

1. 环境准备（[examples/python/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/README.md)）：安装 `requirements.txt`、配套版本的 `pytorch` 与 `torch_npu`，`source ${HOME}/Ascend/cann/set_env.sh`，并确认 `hixl.so` 在 `PYTHONPATH` 覆盖范围内（见 4.3 实践）。
2. 终端 1 启动 server：

   ```bash
   python3 examples/python/hixl_d2rd_multiproc_sample.py --role server
   ```

3. 终端 2 启动 client：

   ```bash
   python3 examples/python/hixl_d2rd_multiproc_sample.py --role client
   ```

4. 观察两侧日志后，把样例中的 `BUF_SIZE` 改小为 `4 * 1024`（`BLOCK_SIZE` 不变），重跑并记录 `BLOCK_COUNT` 变化与校验结果（改完记得还原，不要提交）。

**需要观察的现象**：server 日志依次出现 `engine initialized`、`buffer at 0x...`、`RegisterMem success, handle=0x...`、`sent buffer addr`；client 日志依次出现 `received remote addr`、`Connect success`、`TransferSync READ completed`、`Verify success — all bytes match 0xAA`。修改 `BUF_SIZE` 后 `BLOCK_COUNT` 应变为 256（4MB/16KB），校验仍通过。

**预期结果**：双进程退出码 0，client 打印 `Sample finished successfully`。本实践需要两台互通 device（或单机 device 0/2 互通的 A3 环境）；无硬件环境时**待本地验证**，替代实践：通读样例，把其中每次 `hixl.*` 调用登记到 4.2.3 的接口对照表中，标注该调用属于五组接口（生命周期/内存/链路/传输/通知）中的哪一组。

#### 4.4.5 小练习与答案

**练习 1**：样例为什么用 `struct.pack("!Q", dev_addr)` 发地址，而不是直接 `str(dev_addr)`？

**答案**：TCP 是字节流，定长 8 字节大端无符号整数（`!Q`）配合 [_recv_exact](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_d2rd_multiproc_sample.py#L128-L135) 的循环收满，天然免疫粘包/半包；文本协议则需要额外分隔符与转义。控制面协议设计在 C++ 与 Python 样例间保持一致（同为 8 字节 + 端口偏移 1000）。

**练习 2**：server 端的 `_wait_client_disconnect` 为什么要等 client 断开才退出？

**答案**：HIXL 的被动方必须保持存活到主动方完成传输（u1-l3 的「done 信号保证引擎存活」合同）。server 的 engine 一旦 `finalize()`，远端内存授权与链路全部失效，client 还在 READ 就会失败。该函数带 5 秒超时兜底：client 崩溃时 server 也能退出而不是永久挂起。

**练习 3**：若把 client 的 `transfer_sync` 换成 `transfer_async` + `get_transfer_status` 轮询，需要额外引入哪些 Python 侧对象？

**答案**：需要 `hixl.TransferArgs()`（可省，`transfer_async` 有默认参数）接收返回的 `(status, req_id)` 二元组，然后用 `engine.get_transfer_status(req_id)` 轮询直到返回 `(hixl.SUCCESS, hixl.TransferStatus.COMPLETED)`。req_id 是 `uintptr_t` 整数，直接保存即可——对照 u2-l5：查询返回 SUCCESS 不等于传输成功，必须检查第二个元素的终态。

## 5. 综合实践

**任务：为 HIXL Python 绑定写一份「API 速查卡」，并用一次导入冒烟测试验证它。**

1. **源码推导**（无硬件可完成）：以 [hixl_py.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc) 为唯一事实来源，产出三张表——①`hixl.Hixl` 的 17 个方法（Python 名/C++ 名/参数默认值/返回形态）；②6 个数据类的构造参数与字段（标注哪些字段是 property 翻译的指针）；③模块级常量与枚举全集（标注哪些枚举值被导出到模块命名空间）。
2. **运行验证**（需构建环境）：在 Python 里 `import hixl` 后用 `dir()` 与 `help()` 逐项核对三张表；再执行一次不触碰硬件的安全调用 `hixl.get_capability(hixl.FeatureType.AUTO_CONNECT)`，确认返回 `(0, 1)` 形态的二元组（Status SUCCESS + FEATURE_SUPPORTED；具体数值**待本地验证**）。
3. **端到端**（需昇腾环境）：按 4.4.4 跑通双进程样例，并在 client 侧把 `transfer_sync` 改写为 `transfer_async` + 轮询（参考练习 3 答案），对比两种写法的代码行数与日志时序，体会绑定层对异步 API 的暴露方式。

这个任务把本讲三个知识点串起来：注册代码决定 Python 侧形态（4.2）、包装层决定类型翻译规则（4.1）、构建与安装决定 `import` 能否成功（4.3）。

## 6. 本讲小结

- 绑定代码分三层组织：`HixlPy` 包装类做类型翻译（`AscendString`↔`std::string`、`void*`↔`uintptr_t`、出参↔`pair`）与互斥保护；四个 `Register*` 函数分别注册常量、枚举、数据类与引擎类；`PYBIND11_MODULE(hixl, m)` 是唯一入口。
- 每个 Python 方法都挂 `py::call_guard<py::gil_scoped_release>()`，析构函数也主动放锁——长耗时 C++ 调用不阻塞其他 Python 线程。
- Python 侧 `hixl` 模块 = `Hixl` 类（17 个 snake_case 方法，对应 C++ 五组公开 API）+ 6 个数据类 + 5 个枚举 + 9 个错误码/7 个选项键/2 个能力值常量 + 模块函数 `get_capability`。
- `hixl.so` 由 `add_library(hixl MODULE ...)` 编译（`PREFIX ""` + `SUFFIX ".so"` 保证命名，仅在非 ENABLE_TEST 构建时编译），安装脚本直接复制到 site-packages 并建 `hixl/hixl.so` 软链接；而 `llm_datadist` 走 CMake 调 `setup.py bdist_wheel` 的 whl 流水线——两种打包方式服务于「裸扩展模块」与「Python 包+附属 so」两种形态。
- `hixl_d2rd_multiproc_sample.py` 用 torch_npu 的 `data_ptr()` 整数直接驱动注册与传输，控制面 TCP（引擎端口+1000、8 字节大端地址）与 C++ 样例完全同构。

## 7. 下一步学习建议

- 下一讲 u7-l2 将学习 **LLM-DataDist 的 Python 接口**（`src/python/llm_datadist/`）：whl 安装出的 Python 包结构、`LLMDataDist`/`CacheManager` 等类的用法，以及 `pull_cache_sample.py`、`transfer_cache_async_sample.py` 等更多 Python 样例——可对照本讲的打包流水线理解它的安装产物。
- 若想深挖 pybind11 手法，回读 [hixl_py.cc:273-320](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L273-L320) 中 `def_property` + lambda 做指针翻译的写法，并对照 `src/python/llm_wrapper`、`src/python/metadef_wrapper`（被 llm_datadist whl 打包的两个 wrapper 模块）看同类问题的其他解法。
- 若对端到端业务场景更感兴趣，可直接跳到 u7-l3 的 PD 分离双进程样例，那里会把本讲的传输接口与 u6 的 Push/Pull 语义结合成完整闭环。
