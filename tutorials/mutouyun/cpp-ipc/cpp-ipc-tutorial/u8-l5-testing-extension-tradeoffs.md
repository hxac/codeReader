# 测试体系、扩展点与架构取舍

## 1. 本讲目标

本讲是专家层（U8）的收口篇。前面七单元你已经从「用」到「懂」走完了 libipc 的全部主链路：公共 API、消息通路、无锁队列、共享内存、同步原语、内存子系统、无锁结构。本讲不再讲新的算法，而是回答三个工程层面的问题：

1. **怎么验证它是对的？** —— libipc 的测试体系怎么组织、怎么构建、route/channel 的测试各有什么套路。
2. **库到底编译了哪些组合？** —— `ipc.cpp` 末尾那道「模板显式实例化门」是如何把 `relat × trans` 的 4 种有意义组合裁剪成 3 种、把另外 2 种挡在门外的。
3. **要新增一种通道策略或一个平台，该动哪里？** —— 以及这些设计背后的架构取舍（32 接收者上限、实例化门、无锁 vs 自旋）。

学完本讲，你应该能：看懂 `test-ipc` 的构建与测试写法；说清「实例化门」的作用与代价；评估「启用 TBD 的 unicast 多消费者变体」的真实工作量；并理解库作者在性能、复杂度、API 表面积之间做的权衡。

## 2. 前置知识

本讲默认你已掌握：

- **`relat` / `trans` / `wr` 三件套**（[u2-l1](u2-l1-core-types-and-policy-flags.md)）：`relat::{single,multi}` 描述生产者/消费者多重性，`trans::{unicast,broadcast}` 描述传输方式，`wr<Rp,Rc,Ts>` 把它们打包成一个策略标签。
- **`route` 与 `channel` 只是别名**（[u2-l4](u2-l4-route-vs-channel.md)）：它们都是同一个模板 `chan<Rp,Rc,Ts>` 的 `using`，展开为 `chan_wrapper<wr<...>>`。
- **`prod_cons_impl` 的变体**（[u4-l3](u4-l3-prod-cons-unicast.md)、[u4-l4](u4-l4-prod-cons-broadcast.md)）：单播有 3 条继承链（single-single / single-multi / multi-multi），广播有 2 条（single-multi / multi-multi）。其中**只有 single-single-unicast、single-multi-broadcast、multi-multi-broadcast 三条真正被编译进库**。
- **大消息外部存储**（[u3-l3](u3-l3-large-message-storage.md)）：超大消息走 chunk 仓库 + `storage_id` 票据，回收靠引用计数。
- **共享内存后端分派**（[u5-l2](u5-l2-platform-detection.md)）：`platform.cpp` 用编译期宏 `LIBIPC_OS_*` 在 `shm_win.cpp` / `shm_posix.cpp` 间分流。

如果你对上面任何一条还模糊，建议先回看对应讲义——本讲会直接引用这些结论，不再重证。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 |
| --- | --- |
| `test/CMakeLists.txt` | 测试可执行目标 `test-ipc` 的构建脚本：glob 收集源码、排除归档、链接 gtest 与 ipc |
| `test/test_ipc_channel.cpp` | route/channel 的功能测试集，是「测试模式」的范例 |
| `src/libipc/ipc.cpp` | 库实现核心，其**末尾 846–850 行**就是「实例化门」 |
| `include/libipc/def.h` | `relat`/`trans`/`wr`/`relat_trait` 的定义，是理解「门」的钥匙 |
| `src/libipc/prod_cons.h` | 各变体算法的特化，用来证明「TBD 变体的算法其实已写好」 |
| `include/libipc/ipc.h` | `chan_impl` 静态接口与 `route`/`channel` 别名 |
| `src/libipc/policy.h` / `src/libipc/platform/platform.cpp` | 扩展点：策略选择与平台后端分派 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：①测试目录与构建；②`test_ipc_channel` 测试模式；③模板实例化门；④扩展点与架构取舍。

---

### 4.1 测试目录与构建

#### 4.1.1 概念说明

libipc 是一个库，库本身的「正确」只能靠测试来保证。但跨进程 IPC 的测试有个天然难点：**它需要多个进程协作**。libipc 的处理方式很务实——绝大多数行为其实可以在**单进程多线程**里验证：只要两条 `route`/`channel` 用同一个名字连上，它们就落在同一块共享内存上，发收语义与跨进程完全一致（共享内存本就不区分同进程还是跨进程）。因此测试用 gtest 起若干 `std::thread` 即可，无需 fork。

测试代码本身与库实现**完全分离**：测试不 include 库的内部头（除了 `test/` 自身），只通过公共 `#include "libipc/ipc.h"` 驱动。这意味着测试同时充当了「公共 API 的使用范例」。

#### 4.1.2 核心流程

构建测试的流程是：

1. 顶层 `CMakeLists.txt` 的开关 `LIBIPC_BUILD_TESTS` 为 ON 时，进入 `test/` 子目录（详见 [u1-l2](u1-l2-build-and-run.md) 构建安装篇）。
2. `test/CMakeLists.txt` 把所有 `test_*.cpp` 连同 `test/imp`、`test/mem`、`test/concur` 子目录的源码 glob 到一起，组成**一个**可执行目标 `test-ipc`。
3. 显式排除 `archive/` 下的旧测试，避免过时代码干扰。
4. 链接 `gtest`、`gtest_main`（提供 `main`）、`ipc`（库本体）。
5. 运行 `./test-ipc` 即跑全部用例。

#### 4.1.3 源码精读

测试构建脚本全文只有 37 行，核心是「glob 收集 + 过滤归档 + 链接」三步：

[test/CMakeLists.txt:19-32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/CMakeLists.txt#L19-L32) —— 用 `file(GLOB ...)` 收集 `test_*.cpp` 及三个子目录源码，随后 `list(FILTER ... EXCLUDE REGEX "archive")` 把归档目录剔掉，最后 `add_executable` 生成 `test-ipc`。

注意两点：

- glob 列表里 `test/profiler/*.cpp` 是被注释掉的（`#`），说明性能剖析测试默认不进构建。
- `test/archive/` 下保留着 `test_ipc.cpp`、`test_queue.cpp`、`test_waiter.cpp` 等旧测试，它们被正则 `archive` 排除——这是「新旧测试并存、新代码只认新文件」的常见做法。

链接语句在这里：

[test/CMakeLists.txt:35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/CMakeLists.txt#L35) —— `target_link_libraries(test-ipc gtest gtest_main ipc)`。`gtest_main` 提供入口 `main`，`ipc` 是 [u1-l2](u1-l2-build-and-run.md) 里 `src/CMakeLists.txt` 产出的库目标，传递式带上 `include/` 头目录。

#### 4.1.4 代码实践

**实践目标：在本机构建并运行 `test-ipc`，观察 route/channel 测试是否全绿。**

操作步骤：

1. 在仓库根目录开 `LIBIPC_BUILD_TESTS` 并配置：
   ```bash
   cmake -B build -DLIBIPC_BUILD_TESTS=ON
   cmake --build build -j
   ```
2. 运行：
   ```bash
   ./build/test/test-ipc
   ```
3. 也可只跑某一组用例：
   ```bash
   ./build/test/test-ipc --gtest_filter='RouteTest.*'
   ./build/test/test-ipc --gtest_filter='ChannelTest.*'
   ```

需要观察的现象：每个 `TEST_F` 打印 `[ OK ]` 或 `[ FAILED ]`，最后汇总 `PASSED` 数量；每个用例之间因 `TearDown` 里有 `sleep_for(10ms)`，节奏较慢。

预期结果：route 与 channel 两组用例全部 PASSED。

> 如果环境无 gtest 预置或构建失败，则本步骤为「待本地验证」——构建细节依赖 [u1-l2](u1-l2-build-and-run.md) 描述的 `3rdparty/gtest`。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试只要起多个 `std::thread`、用同名通道就能验证「跨进程」语义？

> 答案：共享内存（`shm_open`/`CreateFileMapping` 产出的命名对象）按名字寻址，**不区分访问者是同进程的不同线程还是不同进程**。同名即落在同一块共享内存、同一个无锁队列上，所以多线程就是多访问者，收发语义与跨进程等价。

**练习 2**：如果把一个新测试文件命名为 `test/foo.cpp`（不带 `test_` 前缀），它会被编译进 `test-ipc` 吗？

> 答案：不会。glob 模式是 `test_*.cpp`，不匹配 `foo.cpp`。要纳入构建必须以 `test_` 开头，或把它放进被 glob 的子目录（`imp/mem/concur`）。

---

### 4.2 test_ipc_channel 测试模式

#### 4.2.1 概念说明

`test_ipc_channel.cpp` 是公共 API 的「标准用法手册」：它把 route（单写多读广播）和 channel（多写多读广播）的生命周期、收发、超时、清理、克隆、多对多都覆盖了一遍。读懂这套测试，等于读懂了库作者期望你怎么用这个库。

它有两个反复出现的套路值得记住：**唯一命名**与**收发配对**。

#### 4.2.2 核心流程

- **唯一命名**：每个用例都用 `generate_unique_ipc_name(...)` 生成互不相同的通道名，避免用例之间因共享内存残留而互相干扰。
- **收发配对**：发送放在一个 `std::thread`、接收放在另一个 `std::thread`，模拟「一端发一端收」的真实场景。
- **广播验证**：一条消息、多个 receiver，断言每个 receiver 都收到了同一条消息——这正是广播语义的核心。
- **资源清理**：每个 fixture 的 `TearDown` 睡 10ms，给共享内存对象的引用计数回收留时间。

#### 4.2.3 源码精读

唯一命名的小工具——一个静态计数器保证进程内每个名字唯一：

[test/test_ipc_channel.cpp:57-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_ipc_channel.cpp#L57-L60) —— `generate_unique_ipc_name` 拼出 `"前缀_ipc_N"`，N 单调递增。

route 的「一写多读广播」测试，是理解广播语义最好的入口：

[test/test_ipc_channel.cpp:414-448](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_ipc_channel.cpp#L414-L448) —— `OneSenderMultipleReceivers`：3 个 receiver 线程各自 `route(name, receiver)` 连上同名通道并 `recv(1000)`，主线程起一个 sender 广播 `"Broadcast"`，最后断言 3 个 `received[i]` 全为 true。这就是 [u4-l4](u4-l4-prod-cons-broadcast.md) 里 `rc_` 读计数位图的最终用户可见效果——每个在线 receiver 各读一份。

channel 的「多写多读」测试更进一步，用了一个手写的 `latch` 做接收方就绪同步：

[test/test_ipc_channel.cpp:534-591](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_ipc_channel.cpp#L534-L591) —— `MultipleSendersReceivers`：2 个 sender 各发 5 条、2 个 receiver。关键断言在末尾——`received_count` 应等于 `num_senders * messages_per_sender * num_receivers`，即「每条消息被每个 receiver 各收一次」，这正是广播（而非单播负载均衡）的铁证。注释也写明了 `// All messages should be received (broadcast mode)`。

`latch`（C++20 才有 `std::latch`，这里为兼容 C++14 手写）确保 receiver 全部 `connect` 完毕后 sender 才开始发，避免「消息发出去时还没人收」的竞态：

[test/test_ipc_channel.cpp:35-55](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_ipc_channel.cpp#L35-L55) —— 经典的 `mutex` + `condition_variable` 倒计数门闩。

#### 4.2.4 代码实践

**实践目标：仿照测试，写一个最小广播验证。**

操作步骤（源码阅读型实践，无需运行）：

1. 阅读 `OneSenderMultipleReceivers`（上面的链接）。
2. 在纸上推演：若把 `num_receivers` 从 3 改成 **33**，会发生什么？

需要观察的现象：结合 [u2-l4](u2-l4-route-vs-channel.md) 的「32 接收者上限」——连接位图 `cc_t` 是 32 位整数，第 33 个 receiver 的 `connect` 会返回失败（`cc_` 全 1 时 `curr|(curr+1)` 回绕为 0，`next==curr` 返回 0），该 receiver `valid()` 仍可能为真但**收不到广播**。

预期结果：第 33 个 receiver 的 `received[i]` 会一直为 false（或 `recv` 超时返回空 buffer）。这是一个纯推理结论——若要实测，需在能起 33 条线程的环境运行，标为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`MultipleSendersReceivers` 里为什么必须用 `latch` 等 receiver 就绪，而 `OneSenderMultipleReceivers` 只用了 `sleep_for(50ms)`？

> 答案：后者只有 1 个 sender，用 sleep 大致「等接收方上线」即可，竞态概率低且失败影响小；前者是多对多，若 receiver 还没连上 sender 就发，消息可能因「无接收者」被 `force_push` 挤掉或 sender 等待超时，导致断言不稳定。`latch` 是确定性的同步，避免 flaky test。

**练习 2**：测试里的 `check_buffer_content` 期望 `buf.size() == expected.size() + 1`，多出的 1 字节是什么？

> 答案：是 `send(std::string)` 重载多发的那个结尾 `\0`（见 [u1-l4](u1-l4-first-ipc-program.md)）。所以接收到的 buffer 比字符串内容多 1 字节的空终止符。

---

### 4.3 模板实例化门

#### 4.3.1 概念说明

这是本讲最关键的概念。libipc 的通道类型是 `chan<Rp, Rc, Ts>`，理论上 `relat`（2 种）× `relat`（2 种）× `trans`（2 种）= 8 种组合。但去掉语义重复（比如 single-single 在 unicast/broadcast 下行为相近）后，真正有区分度的有意义组合是 4 种单播/广播变体 + 若干退化情形。

**问题**：`chan_wrapper`、`chan_impl`、`detail_impl`、`queue_generator`、`prod_cons_impl`、`elem_array`、`conn_head` 这一整条模板链，都是在「使用处实例化」的头文件模板。如果没有人显式实例化它们，它们就不会被编译进库；而 `route`/`channel` 这两个别名虽然在公共头里，但库本体（`ipc` 静态库）是 `.cpp` 编译的，**必须有某个 `.cpp` 把模板实例化出来，链接器才找得到符号**。

**解决**：`ipc.cpp` 末尾用「显式实例化（explicit instantiation）」语法 `template struct chan_impl<...>;`，把选定组合的整条模板链强制编译进库。这一组语句就是「实例化门」——它决定了**库到底向使用者交付哪几种通道**。

#### 4.3.2 核心流程

`wr<Rp,Rc,Ts>` 是策略标签，`relat_trait` 把它萃取成三个编译期布尔（`is_multi_producer`/`is_multi_consumer`/`is_broadcast`），驱动整条链的算法分派：

```
wr<Rp,Rc,Ts>   ──(实例化门)──▶  chan_impl<wr<...>>          [ipc.cpp:846]
                                        │
                                   policy::choose              [policy.h:16]
                                        │
                              elem_array<prod_cons_impl<Flag>> [elem_array.h]
                                        │
                              prod_cons_impl<Flag> 特化          [prod_cons.h]
```

也就是说：在门上写一行 `template struct chan_impl<wr<X,Y,Z>>;`，就会把 `X,Y,Z` 这一组合对应的 `prod_cons_impl`、`elem_array`、`conn_head`、`queue`、`detail_impl` 全部编译进库。门上没有的组合，库不交付——哪怕它的算法已经写好了。

#### 4.3.3 源码精读

门本身只有 5 行，但信息量极大：

[src/libipc/ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850) —— 三行生效、两行注释为 `// TBD`：

```cpp
template struct chan_impl<ipc::wr<relat::single, relat::single, trans::unicast  >>;
// template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::unicast  >>; // TBD
// template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::unicast  >>; // TBD
template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::broadcast>>;
template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::broadcast>>;
```

读法：

- 第 846 行 `single/single/unicast` —— 最简 SPSC 环形队列（[u4-l3](u4-l3-prod-cons-unicast.md) 的基类），编译进库但**没有公共别名**（无人 `using` 它），属于「内部可用但未对外暴露」。
- 第 849 行 `single/multi/broadcast` —— 就是 `route`。
- 第 850 行 `multi/multi/broadcast` —— 就是 `channel`。
- 第 847、848 行 —— `single/multi/unicast` 与 `multi/multi/unicast`，被注释掉并标 `// TBD`（to be determined）。

公共别名正好对应门里生效的两个广播组合：

[include/libipc/ipc.h:219-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L219-L228) —— `route = chan<single, multi, broadcast>`、`channel = chan<multi, multi, broadcast>`，而 `chan` 又是 `chan_wrapper<wr<Rp,Rc,Ts>>` 的别名（`ipc.h:209`）。

**关键证据：TBD 的算法其实已经写好了。** 看 `prod_cons.h` 的特化清单：

[src/libipc/prod_cons.h:26](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L26) —— `single/single/unicast`（已实例化）。
[src/libipc/prod_cons.h:75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L75) —— `single/multi/unicast`（**算法已写，门没开**）。
[src/libipc/prod_cons.h:106](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L106) —— `multi/multi/unicast`（**算法已写，门没开**）。
[src/libipc/prod_cons.h:196](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L196) 与 [:294](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L294) —— 两个广播变体（已实例化）。

也就是说，门挡住的不是「没写的功能」，而是「写了但没交付」的功能。`chan_impl` 的成员函数都是转发到 `detail_impl<policy_t<Flag>>` 的薄封装：

[src/libipc/ipc.cpp:820-844](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L820-L844) —— `chan_impl::send/recv/try_send/try_recv` 全部一行转发，例如 `send` 调 `detail_impl<policy_t<Flag>>::send(h, data, size, tm)`。`policy_t` 在 [:746](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L746) 定义为 `ipc::policy::choose<ipc::circ::elem_array, Flag>`。所以一旦你在门上多写一行，整条 `policy::choose → prod_cons_impl → elem_array` 链就会为这个新 `Flag` 实例化，符号立即生成。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标：解释为何 unicast 的 `single-multi` / `multi-multi` 被注释为 TBD，并评估启用它们的工作量。**

操作步骤（源码阅读 + 推理型实践）：

1. 打开 [ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)，确认两行 TBD 对应 `prod_cons.h:75` 与 `prod_cons.h:106` 的特化（即算法已存在）。
2. 追问：既然算法有了，为什么不开放？从「语义、验证、API、依赖」四个角度推理（见下方预期结论）。
3. 追问：开放需要改几处？列出改动清单。

**为何标 TBD（推理结论）：**

- **语义偏冷门**：`multi-consumer + unicast` = 「多个接收者抢消息，每条消息只被一个接收者拿走」，这是**工作队列 / 负载均衡**语义。而 libipc 的对外卖点（`route`/`channel`）都是广播——一条消息人手一份。单播多消费者与库的设计主线不重合，缺乏强需求驱动。
- **未经验证**：`// TBD` 字面意思是「待定」，配合 [u4-l3](u4-l3-prod-cons-unicast.md) 讲过的「这两个算法已实现但未编译进库」，说明它们**没有经过测试覆盖**（`test_ipc_channel.cpp` 只测 route/channel）。开放前需要补并发正确性测试（尤其是 multi-multi-unicast 的 commit 协议与 CAS 抢占）。
- **API 表面积**：开放就需要新增公共别名（如 `using route_unicast = chan<single, multi, unicast>`），扩大对外接口，增加长期维护负担。
- **下游依赖需复核**：大消息外部存储的引用计数回收（[u3-l3](u3-l3-large-message-storage.md) 的 `recycle_storage`/`sub_rc`）对单播有特化（恒返回 true，即读一次即回收），需确认在单播多消费者下行为符合预期。

**启用工作量评估（结论）：**

| 改动点 | 工作量 | 说明 |
| --- | --- | --- |
| 取消 [ipc.cpp:847-848](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L847-L848) 注释 | 极小 | 删两个 `//`，整条模板链即实例化 |
| 新增公共别名（可选） | 小 | 在 `ipc.h` 加 1–2 个 `using` |
| 补并发正确性测试 | 中 | 这是主要成本：需仿 `MultipleSendersReceivers` 写单播版，验证「每条消息恰好被一个 receiver 拿走」 |
| 复核大消息/连接位图路径 | 中 | 单播 `conn_head` 走计数不分位、`sub_rc` 单播特化，需端到端验证 |

预期结果：**纯代码改动量极小（两行注释 + 一个别名），真正的工作量在「测试验证」**。这也是「门」存在的价值——它用零成本把「未验证的功能」挡在稳定 ABI 之外。

> 若尝试本地取消注释直接编译，大概率能通过（因为算法已存在），但功能未验证，标为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果直接在用户代码里写 `ipc::chan<relat::multi, relat::multi, trans::unicast> ch(...)`（不改库），能链接成功吗？

> 答案：不能（在用预编译库 `libipc.a` 的情况下）。虽然头文件模板能在用户代码里实例化 `chan_wrapper` 和 `chan_impl` 的声明，但 `chan_impl` 的**成员函数定义**只在 `ipc.cpp` 里，而 `ipc.cpp` 没有为这个 `Flag` 实例化它，链接器找不到符号，报 undefined reference。这正是「门」的约束力来源。除非用户把库源码一起编译，或自己在某个 `.cpp` 里 `template struct chan_impl<wr<multi,multi,unicast>>;`（但 `chan_impl` 成员在匿名命名空间外的 `.cpp`，用户无法直接实例化库内部定义）——实际只能改库的门。

**练习 2**：为什么库作者不直接把 8 种组合全实例化，省得留门？

> 答案：每多一种组合，就多编译一整条 `prod_cons_impl → elem_array → conn_head` 模板链，增加编译时间与库体积；更重要的是，未经验证的组合进了库，就成了「事实上的对外承诺」，bug 会算到库头上。门 = 只交付「测过且有需求」的组合，是质量与表面积的权衡。

---

### 4.4 扩展点与架构取舍

#### 4.4.1 概念说明

理解了「门」，就理解了 libipc 的扩展哲学：**算法层是开放的（模板特化随便加），交付层是收敛的（门只放行少数组合）**。本模块把这套哲学推广到两类扩展：新增通道策略、新增平台后端。并总结贯穿全库的三个架构取舍。

#### 4.4.2 核心流程

**新增通道策略**（例如开放 TBD 变体，或自创一种 `relat`/`trans`）：

1. 在 `prod_cons.h` 写出（或确认已有）目标 `wr<...>` 的 `prod_cons_impl` 特化。
2. 在 `ipc.cpp:846` 的门上加一行 `template struct chan_impl<wr<...>>;`。
3. 在 `ipc.h` 加 `using` 别名（可选）。
4. 补测试。

**新增平台后端**（例如适配一个新 OS）：

1. 在 `detect_plat.h` 的检测链里加该平台的宏判定，翻译成 `LIBIPC_OS_*`。
2. 在 `platform.cpp` 的 `#elif` 链里 `#include` 你的 shm 后端 `.cpp`。
3. 实现同步原语后端（mutex/condition/semaphore）放在对应 `platform/<os>/` 目录。

#### 4.4.3 源码精读

策略分派的入口是 `policy::choose`，它把 `Flag` 映射到具体的 `elem_array<prod_cons_impl<Flag>>`：

[src/libipc/policy.h:16-22](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L16-L22) —— `choose<circ::elem_array, Flag>` 内嵌 `elems_t = circ::elem_array<ipc::prod_cons_impl<flag_t>, DataSize, AlignSize>`。这是「Flag → 算法」的编译期路由表，新增特化即自动接入。

平台后端的分派是同构的思路，只是用预处理宏而非模板：

[src/libipc/platform/platform.cpp:3-9](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.cpp#L3-L9) —— 按 `LIBIPC_OS_*` 只 `#include` 一份 shm 后端，未命中则 `#error`。新增平台就是在这条链上加一个 `#elif`。

`relat_trait` 是所有这些分派的「编译期布尔源」，定义在宪法文件 `def.h`：

[include/libipc/def.h:56-67](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L56-L67) —— `relat_trait<wr<Rp,Rc,Ts>>` 萃取出三个布尔；并对 `Policy<Flag>` 做了「剥壳」特化，使外层包装（如 `policy::choose` 的产物）能透传到底层算法。这保证「不管策略怎么包，最终都认 `wr`」。

**三大架构取舍**（贯穿全库，此处汇总）：

1. **32 接收者上限（vs. 无限接收者）**：广播需要为每个接收者占 1 bit 以标记「是否已读」，`cc_t = uint32`（[u2-l4](u2-l4-route-vs-channel.md)、[u4-l2](u4-l2-elem-array-conn-head.md)）。换取的是：槽位回收判定只需一个 32 位原子的位运算，无需链表或额外计数。代价是单条广播通道最多 32 个 receiver。这是「用固定上限换 O(1) 无锁回收」。
2. **实例化门（vs. 全量编译）**：如 4.3 所述，只交付 3 种组合，换取更小的库、更快的编译、更收敛的对外承诺。代价是 unicast 多消费者变体虽已实现却不可用。
3. **无锁算法 + 自旋退避（vs. 纯内核阻塞）**：环形队列用原子操作（CAS、release/acquire）实现无锁（[u4-3](u4-l3-prod-cons-unicast.md)、[u4-l4](u4-l4-prod-cons-broadcast.md)），等待时先自旋若干轮再转条件变量阻塞（[u3-l4](u3-l4-wait-model.md)、[u6-l1](u6-l1-spin-rw-lock.md)）。换取的是高吞吐低延迟；代价是空转时消耗 CPU、以及无锁算法的正确性证明与实现难度（详见 [u8-l1](u8-l1-memory-ordering.md) 的内存序分析）。

#### 4.4.4 代码实践

**实践目标：追踪「新增一个 unicast 变体」需要落到的全部文件，画出改动热力图。**

操作步骤（源码阅读型）：

1. 假设要启用 `multi/multi/unicast`，从门 [ipc.cpp:848](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L848) 出发，删掉注释。
2. 反向追依赖：`chan_impl<Flag>` → `detail_impl<policy_t<Flag>>`（[ipc.cpp:746](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L746)）→ `policy::choose`（[policy.h:16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/policy.h#L16)）→ `prod_cons_impl<multi,multi,unicast>`（[prod_cons.h:106](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L106)）。
3. 列出需要复核的旁路：连接位图（`conn_head` 单播特化，[elem_def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h)）、大消息回收（`sub_rc` 单播特化，[u3-l3](u3-l3-large-message-storage.md)）。

需要观察的现象：核心算法链（门 → policy → prod_cons）改动为 0（已存在）；旁路（conn_head、chunk 回收）走的是 `relat_trait::is_broadcast` 特化，单播路径天然存在，但缺测试。

预期结果：改动热力集中在「门 1 行 + 测试若干」，再次印证门是「零成本开关」。

#### 4.4.5 小练习与答案

**练习 1**：`policy::choose` 为什么用模板特化，而 `platform.cpp` 用预处理宏？

> 答案：策略分派是**编译期、类型层面**的（选哪种 `prod_cons_impl`），用模板特化更类型安全、可被 IDE/编译器检查；平台分派是**翻译单元层面**的（这个 `.cpp` 在哪个 OS 上编译），只能在预处理阶段用宏决定 include 哪份后端，因为两份后端依赖不同系统头、不能同时编译。

**练习 2**：如果要把广播的 32 接收者上限提到 64，最小改动是什么？会有什么连锁影响？

> 答案：把 `cc_t` 从 `uint32` 改成 `uint64`（定义在 `circ/elem_def.h`）。连锁影响：连接位图、`rc_` 读计数位域布局（[u4-l4](u4-l4-prod-cons-broadcast.md) 的 epoch/计数位宽需重新划分）、`id_pool` 的 `max_count`（受 `large_msg_cache` 与 `uint8_max` 约束，需同步放宽）都要复核。这是「牵一发动全身」，也是 32 上限被固化的原因之一。

---

## 5. 综合实践

**任务：为「启用 multi/multi/unicast 工作队列变体」写一份最小落地清单与验证测试草案。**

要求把本讲四个模块串起来：

1. **构建侧（模块 1）**：确认开关 `LIBIPC_BUILD_TESTS=ON` 能跑通 `test-ipc`，作为后续验证的基础设施。
2. **测试侧（模块 2）**：仿照 `MultipleSendersReceivers`（[test_ipc_channel.cpp:534](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_ipc_channel.cpp#L534)），写一个 `UnicastWorkQueue` 用例：N 个 sender 共发 M 条消息、K 个 receiver 抢收，断言「每条消息恰好被一个 receiver 拿走」（总收到数 == M，而非 M×K），以区别于广播。
3. **门侧（模块 3）**：取消 [ipc.cpp:848](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L848) 的注释，在 `ipc.h` 加 `using work_queue = chan<relat::multi, relat::multi, trans::unicast>;`。
4. **取舍侧（模块 4）**：在清单里写明需复核的旁路（`conn_head` 单播、`sub_rc` 单播特化、`id_pool` 上限），并评估是否值得为之扩大对外 API。

产出物：一份 markdown 清单，包含「改动文件 / 行号 / 验证用例 / 风险」四列。本任务不要求真正改库源码（禁止修改源码），只产出方案。验证步骤标为「待本地验证」。

## 6. 本讲小结

- **测试体系**：`test-ipc` 用 glob 收集 `test_*.cpp`、排除 `archive/`、链接 `gtest gtest_main ipc`；跨进程语义靠「同名通道 + 多线程」在单进程内验证。
- **测试模式**：唯一命名（`generate_unique_ipc_name`）+ 收发配对线程 + `latch` 同步；广播用例断言「每个 receiver 各收一份」，是 `rc_` 读计数的用户侧铁证。
- **实例化门**：`ipc.cpp:846-850` 用 `template struct chan_impl<wr<...>>;` 只交付 3 种组合；两行 `// TBD` 对应的算法其实已存在于 `prod_cons.h:75/106`，被门挡住的是「未验证、无别名、缺需求」。
- **扩展哲学**：算法层开放（加 `prod_cons_impl` 特化即接入 `policy::choose`），交付层收敛（门把关）；平台后端同理，只是用预处理宏在 `platform.cpp` 分流。
- **三大取舍**：32 接收者上限（固定上限换 O(1) 无锁回收）、实例化门（小库与收敛承诺换功能可用性）、无锁+自旋退避（高吞吐换 CPU 占用与实现复杂度）。

## 7. 下一步学习建议

本讲是学习手册的终篇。建议你：

1. **回溯串联**：挑一个完整调用链（如 `send` 从 `chan_wrapper::send` 到 `prod_cons_impl::push`），对照 [u3-l1](u3-l1-send-recv-data-path.md) → [u4-1](u4-l1-queue-abstraction.md) → [u4-4](u4-l4-prod-cons-broadcast.md) → [u8-1](u8-l1-memory-ordering.md) 逐层复核，检验自己能否一次讲清。
2. **动手实验**：按综合实践，真正起一份 fork，开放一个 unicast 变体并补测试——这是把「读懂」变成「会改」的关键一步。
3. **横向阅读**：对照 `test/archive/` 下的旧测试（如 `test_queue.cpp`、`test_waiter.cpp`），看库的历史演进，理解为何旧测试被归档、新测试如何重组。
4. **贡献方向**：若你要给库提 PR，最有价值的往往是「补 TBD 变体的并发正确性测试」或「新平台后端适配」——这两者都已被本讲的扩展点分析覆盖。

至此，你已从「跑通 demo」走到「看懂无锁算法、评估架构取舍」。libipc 的全部主链路，你应该都能带着源码行号讲给别人听了。
