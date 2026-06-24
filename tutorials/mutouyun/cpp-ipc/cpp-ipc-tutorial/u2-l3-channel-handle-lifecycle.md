# chan_wrapper 与 chan_impl：句柄生命周期

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `chan_impl` 与 `chan_wrapper` 各自的职责：前者是「按策略分派的静态接口表」，后者是「持有状态的 RAII 句柄」。
- 理解 `handle_t`（即 `void*`）这种不透明句柄如何配合 PIMPL 把类型擦除掉。
- 说清 `sender` / `receiver` 两个模式位如何用一个位运算 `mode & receiver` 被翻译成 `start_to_recv` 布尔量，从而驱动连接行为。
- 用 `connect` / `reconnect` 在「同一个句柄」上切换收发角色，并解释 `recv_count()` 为什么会随之变化。
- 区分四种资源清理动作：析构、`release`、`clear`、`clear_storage`，知道何时用哪一个。

本讲只看「句柄这一层的生命周期」，不展开底层共享内存、队列元素和无锁算法（那是 U3、U4、U5 的事）。

## 2. 前置知识

阅读本讲前，建议你已经掌握（见 u1-l4、u2-l1）：

- **route / channel 是同一个模板 `chan` 的两个预设**：`chan` 本质是 `chan_wrapper<ipc::wr<Rp, Rc, Ts>>`，`route` 和 `channel` 只是 `wr` 标签不同。所以「句柄生命周期」对二者完全通用。
- **RAII**：对象构造即获取资源、析构即释放资源。本讲的 `chan_wrapper` 就是一个 RAII 类型。
- **PIMPL（Pointer to IMPLementation）**：对外只暴露一个指针，把真正的实现细节藏在 `.cpp` 里。`buff_t`（u2-l2）已经见过这个手法，这里 `handle_t` 是同样的思路。
- **模式位**：库定义了 `enum { sender, receiver }`，本讲会反复用到它。

一个**关键编码技巧**先点破，后面会反复用到：

```text
enum : unsigned { sender, receiver };   // sender == 0, receiver == 1
```

因为 `sender == 0`、`receiver == 1`，所以「receiver 位」就是数值的最低位。库到处用 `mode & receiver` 来「提取 receiver 位」——结果非 0 即代表「我要当接收者」。这是本讲最容易看漏的一行代码，记住它能解释后面几乎所有行为。

## 3. 本讲源码地图

本讲几乎全部围绕一个头文件展开，再配合它的实现 `.cpp`：

| 文件 | 作用 | 本讲用到 |
| --- | --- | --- |
| [include/libipc/ipc.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h) | 公共 API：`chan_impl`、`chan_wrapper`、`chan`/`route`/`channel` 别名 | 几乎全部 |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | `chan_impl` 各静态方法的真实实现 + `detail_impl<Policy>` 桥接层 | 实现细节 |
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | `prefix`、`wr<>` 标签、`relat`/`trans` 枚举、`invalid_value` 常量 | 类型约定 |

一句话记忆：**`chan_wrapper` 是你用的，`chan_impl` 是它内部调的，`detail_impl` 是 `chan_impl` 再往下调的真正干活的人。**

## 4. 核心概念与源码讲解

### 4.1 chan_impl：按策略分派的静态接口层

#### 4.1.1 概念说明

`chan_impl<Flag>` 是一个**只装静态函数的 struct**，它没有数据成员。你可以把它理解成一张「函数表」：给定一个策略标签 `Flag`（例如 `wr<relat::multi, relat::multi, trans::broadcast>`），它就提供一整套针对该策略的 `init_first / connect / reconnect / disconnect / destroy / send / recv / ...` 操作。

为什么是「静态函数 + 不透明句柄」而不是普通成员函数？因为 libipc 要做**类型擦除**：对外只暴露一个 `void*` 句柄（`handle_t`），把所有模板参数（`Flag`）藏到 `.cpp` 的显式实例化里。这样：

- 公共头 `ipc.h` 不必把 `conn_info_t`、`queue_t` 这些重量级模板暴露给用户，编译期依赖小、ABI 稳定。
- 真正的连接信息对象（`conn_info_t`）只在 `.cpp` 内部被 `new` 出来，外部拿到的只是 `void*`。

#### 4.1.2 核心流程

`chan_impl` 把每个公共操作转发给真正的实现类 `detail_impl<policy_t<Flag>>`：

```text
用户代码
   │  chan_wrapper::send(...)
   ▼
chan_impl<Flag>::send(handle, ...)        ← 头文件里的静态声明
   │
   ▼
detail_impl<policy_t<Flag>>::send(...)    ← .cpp 里的真正实现
   │
   ▼
queue_of(h)->...                          ← 操作共享内存里的队列
```

`policy_t<Flag>` 在 `.cpp` 里被定义为 `ipc::policy::choose<ipc::circ::elem_array, Flag>`（见 [ipc.cpp:745-746](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L745-L746)），负责把策略标签翻译成具体的元素数组类型。这些底层细节本讲不展开，你只需记住「`chan_impl` 是转发层」。

#### 4.1.3 源码精读

`chan_impl` 的全部静态声明集中在 [include/libipc/ipc.h:20-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L20-L48)。注意它第一行就是 `template <typename Flag>`，并且所有方法都是 `static`：

```cpp
template <typename Flag>
struct LIBIPC_EXPORT chan_impl {
    static ipc::handle_t init_first();
    static bool connect   (ipc::handle_t * ph, char const * name, unsigned mode);
    static bool reconnect (ipc::handle_t * ph, unsigned mode);
    static void disconnect(ipc::handle_t h);
    static void destroy   (ipc::handle_t h);
    // ...
    static void release(ipc::handle_t h) noexcept;   // 不等待断连就释放
    static void clear  (ipc::handle_t h) noexcept;   // 强制清理依赖的共享内存
    static void clear_storage(char const * name) noexcept;
    // ...
    static bool   send(ipc::handle_t h, void const * data, std::size_t size, std::uint64_t tm);
    static buff_t recv(ipc::handle_t h, std::uint64_t tm);
};
```

要点：

- `connect` / `reconnect` 的第一个参数是 `ipc::handle_t * ph`（**句柄的地址**），因为它们可能要给句柄赋值（首次连接时要 `new` 出连接信息对象）。
- `disconnect` / `destroy` / `send` / `recv` 的第一个参数是 `ipc::handle_t h`（**句柄本身**），因为它们只读写已有对象、不改句柄指针。

转发实现几乎是一一对应的样板。例如 `send` 和 `recv`（[ipc.cpp:826-834](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L826-L834)）：

```cpp
template <typename Flag>
bool chan_impl<Flag>::send(ipc::handle_t h, void const * data, std::size_t size, std::uint64_t tm) {
    return detail_impl<policy_t<Flag>>::send(h, data, size, tm);
}
```

**显式实例化门**（重要，承接 u2-l1）：在文件末尾 [ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)，库只编译了 3 种 `wr` 组合：

```cpp
template struct chan_impl<ipc::wr<relat::single, relat::single, trans::unicast  >>;
// template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::unicast  >>; // TBD
// template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::unicast  >>; // TBD
template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::broadcast>>;
template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::broadcast>>;
```

也就是说：虽然算法层面单播（unicast）的多消费者版本已特化，但 `chan_impl` 并没有为它们生成机器码（注释 `TBD`）。所以你实际能用的句柄只有三种策略：单写单读单播（`route` 的单播基础）、`route`、`channel`。你若试图 `ipc::chan<relat::multi, relat::multi, trans::unicast>` 实例化一个 `chan_wrapper`，链接期会报「找不到符号」。

#### 4.1.4 代码实践

**实践目标**：确认「显式实例化门」决定了哪些 `wr` 组合真正可用。

**操作步骤**：

1. 打开 [ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)，数出未被注释的 `template struct chan_impl<...>` 有几行。
2. 对照 [include/libipc/def.h:53-54](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L53-L54) 的 `wr<Rp,Rc,Ts>`，列出这 3 行对应的 `Rp/Rc/Ts`。
3. 想一想：如果写 `ipc::chan<relat::multi, relat::multi, trans::unicast> ch;`，会编译过吗？能链接过吗？

**预期结果**：编译能过（模板语法正确），但**链接失败**，因为 `chan_impl<wr<multi, multi, unicast>>` 没有实例化。这是模板库里很经典的「算法已写好、门未打开」现象。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connect` 用 `handle_t*`（指针的指针），而 `send` 用 `handle_t`？

> **答**：`connect` 首次调用时需要把新 `new` 出来的连接信息对象的地址写回给调用者，所以要传句柄的地址以便修改它；`send` 只是读取已有对象、不改句柄本身，传值即可。

**练习 2**：`chan_impl` 里为什么没有数据成员、全是 `static`？

> **答**：因为它要配合类型擦除——真正的状态存在 `void*` 句柄指向的 `conn_info_t` 里，`chan_impl` 只是一张「按 `Flag` 分派到 `detail_impl`」的静态函数表，自身不需要存任何状态。

---

### 4.2 chan_wrapper：持有状态的 RAII 句柄

#### 4.2.1 概念说明

`chan_wrapper<Flag>` 才是用户真正接触的类型。它在 `chan_impl` 那张静态函数表之上，包了**三个数据成员**，负责记录「这个句柄当前是什么状态」：

```cpp
ipc::handle_t h_ = detail_t::init_first();   // 不透明句柄
unsigned mode_   = ipc::sender;              // 当前角色
bool connected_  = false;                    // 是否已连上
```

有了这三个成员，`chan_wrapper` 就成了一个真正的 RAII 类型：构造时建立连接、析构时销毁句柄，并能在生命周期内查询和切换状态。

#### 4.2.2 核心流程

```text
默认构造  ──► h_ = init_first() (返回 nullptr) , mode_ = sender, connected_ = false
   │
带名构造  ──► 调用 connect(name, mode)，成功则 connected_ = true
   │
使用中    ──► send / recv / reconnect / recv_count / valid / mode ...
   │
析构      ──► detail_t::destroy(h_)   ← 自动断连 + 释放
```

`chan` / `route` / `channel` 三个名字都只是 `chan_wrapper` 的别名（[ipc.h:208-228](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L208-L228)）：

```cpp
template <relat Rp, relat Rc, trans Ts>
using chan = chan_wrapper<ipc::wr<Rp, Rc, Ts>>;
using route   = chan<relat::single, relat::multi, trans::broadcast>;
using channel = chan<relat::multi , relat::multi, trans::broadcast>;
```

所以你写的 `ipc::channel`，编译期就是 `chan_wrapper<wr<multi, multi, broadcast>>`，本讲后面所有内容对它一视同仁。

#### 4.2.3 源码精读

**三个成员与默认值**在 [ipc.h:52-57](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L52-L57)。注意 `h_` 的默认值来自 `init_first()`——它只做一次性的静态初始化（初始化 `waiter` 基础设施），然后返回 `nullptr`（[ipc.cpp:752-756](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L752-L756)）：

```cpp
template <typename Flag>
ipc::handle_t chan_impl<Flag>::init_first() {
    ipc::detail::waiter::init();
    return nullptr;
}
```

也就是说，**默认构造的 `chan_wrapper` 的 `h_` 是 `nullptr`，`valid()` 为 `false`**，你必须再调用 `connect(name, mode)` 才真正连上。

**带名构造函数**（[ipc.h:62-64](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L62-L64)）把连接动作放进成员初始化列表：

```cpp
explicit chan_wrapper(char const * name, unsigned mode = ipc::sender)
    : connected_{this->connect(name, mode)} {
}
```

**析构函数**（[ipc.h:75-77](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L75-L77)）只做一件事——销毁句柄：

```cpp
~chan_wrapper() {
    detail_t::destroy(h_);
}
```

而 `chan_impl::destroy`（[ipc.cpp:778-782](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L778-L782)）会**先断连再释放**：

```cpp
void chan_impl<Flag>::destroy(ipc::handle_t h) {
    disconnect(h);                         // 优雅断连：通知对端、清理接收位
    detail_impl<policy_t<Flag>>::destroy(h);  // mem::$delete 释放本地对象
}
```

**访问器**（[ipc.h:116-130](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L116-L130)）都是薄封装：

```cpp
ipc::handle_t handle() const noexcept { return h_; }
bool     valid() const noexcept { return (handle() != nullptr); }
unsigned mode()  const noexcept { return mode_; }
chan_wrapper clone() const { return chan_wrapper { name(), mode_ }; }
```

- `valid()`：句柄非空即有效。
- `mode()`：返回当前角色（`sender`=0 或 `receiver`=1）。
- `clone()`：**用同名+同模式重新构造一个全新句柄**——这是一个独立的连接。例如克隆一个 receiver 会得到「第二个接收者」，对广播场景很有用。

**move 语义**（[ipc.h:70-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L70-L88)）通过 `swap` 交换三个成员，保证 move 后源对象变为默认状态、不会双重释放。赋值运算符也走 `swap`（copy-and-swap 惯用法）。

#### 4.2.4 代码实践

**实践目标**：观察 `chan_wrapper` 的状态机。

**操作步骤**：

```cpp
#include "libipc/ipc.h"
#include <iostream>

int main() {
    ipc::channel a;                                  // 默认构造
    std::cout << "default: valid=" << a.valid()      // 预期 0
              << " mode=" << a.mode() << "\n";       // 预期 0 (sender)

    ipc::channel b { "demo-state", ipc::receiver };  // 带名构造
    std::cout << "named:   valid=" << b.valid()      // 预期 1
              << " mode=" << b.mode() << "\n";       // 预期 1 (receiver)

    ipc::channel c = std::move(b);                   // move
    std::cout << "after move: src.valid=" << b.valid()  // 预期 0（源被掏空）
              << " dst.valid=" << c.valid() << "\n";    // 预期 1
    return 0;
}
```

**需要观察的现象**：默认构造的对象 `valid()` 为假；带名构造后为真；move 之后源对象变空、目标对象接管连接。

**预期结果**：见注释。具体数值**待本地验证**（需先按 u1-l2 构建出 `ipc` 库再编译本文件并链接）。

#### 4.2.5 小练习与答案

**练习 1**：`clone()` 和 move 构造，都会产生一个「能用的句柄」，它们有什么本质区别？

> **答**：`move` 是**转移**同一个连接的所有权（源句柄变空，连接不增加）；`clone()` 是**新建**一个独立连接（用相同的 name 和 mode 再 `connect` 一次），克隆出的 receiver 在共享内存里会再占一个连接位，广播时它也会收到消息。

**练习 2**：默认构造的 `chan_wrapper` 调用 `send()` 会发生什么？

> **答**：`h_` 是 `nullptr`，`detail_impl::send` 里 `queue_of(h)` 返回 `nullptr`，函数打印错误日志并返回 `false`。所以未连接就发消息不会崩，只是失败。

---

### 4.3 connect / reconnect：模式切换

#### 4.3.1 概念说明

「模式」（mode）决定你在这个通道里扮演发送者还是接收者。库用一个巧妙的位编码把模式压进一个 `unsigned`：

- `sender == 0`，`receiver == 1`。
- 真正驱动行为的不是 mode 本身，而是 **`mode & receiver`** 这一位——它就是 `start_to_recv` 布尔量。

由于 `sender` 是 0、`receiver` 是 1，最低位（receiver 位）的值就是「是否当接收者」。`chan_impl::connect` / `reconnect` 在转发给 `detail_impl` 时，都会先做这个位运算（[ipc.cpp:759-771](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L759-L771)）：

```cpp
bool chan_impl<Flag>::connect(ipc::handle_t * ph, char const * name, unsigned mode) {
    return detail_impl<policy_t<Flag>>::connect(ph, name, mode & receiver);  // 提取 receiver 位
}
```

`connect` 与 `reconnect` 的分工：

- **`connect(name, mode)`**：可能要**新建**连接信息对象（首次连接），并连到指定通道名。会先 `disconnect` 清掉旧连接。
- **`reconnect(mode)`**：句柄已存在，只在**同一个连接对象**上切换收发角色，不换通道名。

#### 4.3.2 核心流程

**connect 的流程**（`chan_wrapper::connect` → `chan_impl::connect` → `detail_impl::connect` → `detail_impl::reconnect`）：

```text
chan_wrapper::connect(name, mode)
  ├─ name 为空？ ── 是 ──► return false
  ├─ disconnect(h_)              // 清旧连接（h_ 为空时无效操作）
  └─ chan_impl::connect(&h_, name, mode)
       └─ detail_impl::connect(ph, name, mode & receiver)
            ├─ *ph == nullptr ? ── 是 ──► *ph = $new<conn_info_t>(name)  // 首次：建对象
            └─ reconnect(ph, start_to_recv = mode & receiver)
```

**reconnect 的核心分支**（[ipc.cpp:481-502](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L481-L502)）：

```text
detail_impl::reconnect(ph, start_to_recv)
  ├─ info_of(*ph)->init()        // 幂等：初始化 waiter / acc / queue（仅首次真正干活）
  ├─ start_to_recv == true（当接收者）:
  │     ├─ que->shut_sending()           // 不再当发送者
  │     ├─ que->connect() 成功?          // 占一个接收者连接位
  │     │     └─ cc_waiter_.broadcast(); return true   // 通知发送方「有新接收者了」
  │     └─ 否则 return false
  └─ start_to_recv == false（当发送者）:
        ├─ 若 que->connected(): disconnect_receiver()  // 撤销之前的接收者位
        └─ return que->ready_sending()                 // 标记为发送者
```

一个**值得注意的默认值陷阱**：

- 带名构造函数的默认 mode 是 `ipc::sender`（[ipc.h:62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L62)）。
- 而 `connect()` 方法的默认 mode 是 `ipc::sender | ipc::receiver`（[ipc.h:135](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L135)）。

因为 `sender | receiver == 0 | 1 == 1 == receiver`，所以**「`ipc::channel c{"x"}`」默认是发送者，但「先默认构造再 `c.connect("x")`」默认却是接收者**。同一个名字、两种写法、不同角色。demo 里（如 [send_recv/main.cpp:18,29](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/send_recv/main.cpp#L18-L29)）都显式写了 `ipc::sender` / `ipc::receiver`，正是为了避免这种隐晦差异。

#### 4.3.3 源码精读

`chan_wrapper::connect` / `reconnect` 的封装（[ipc.h:135-153](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L135-L153)）：

```cpp
bool connect(char const * name, unsigned mode = ipc::sender | ipc::receiver) {
    if (name == nullptr || name[0] == '\0') return false;
    detail_t::disconnect(h_);                                  // 清旧连接
    return connected_ = detail_t::connect(&h_, name, mode_ = mode);
}

bool reconnect(unsigned mode) {
    if (!valid()) return false;                                // 没句柄直接失败
    if (connected_ && (mode_ == mode)) return true;            // 角色没变，免操作
    return connected_ = detail_t::reconnect(&h_, mode_ = mode);
}
```

注意 `reconnect` 有两个快速返回：句柄无效则失败；角色本就没变则直接成功（避免重复 connect）。真正的切换在 `detail_impl::reconnect`，其分支已在 4.3.2 给出，关键代码（[ipc.cpp:481-502](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L481-L502)）：

```cpp
static bool reconnect(ipc::handle_t * ph, bool start_to_recv) {
    auto que = queue_of(*ph);
    if (que == nullptr) return false;
    info_of(*ph)->init();                       // 幂等初始化
    if (start_to_recv) {                        // 当接收者
        que->shut_sending();
        if (que->connect()) {                   // 占接收者位（不会重复占）
            info_of(*ph)->cc_waiter_.broadcast();
            return true;
        }
        return false;
    }
    // 当发送者：若之前是接收者，先撤销
    if (que->connected()) info_of(*ph)->disconnect_receiver();
    return que->ready_sending();
}
```

这条线索解释了「为什么 `recv_count()` 会随角色变化」：`recv_count()` 返回的是 `que->conn_count()`，即当前所有已连接的**接收者**数量（[ipc.cpp:508-514](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L508-L514)）。当你是接收者时，你占了一个位、计数包含你；`reconnect` 切成发送者时 `disconnect_receiver()` 撤销了你的接收位，计数就减一。

#### 4.3.4 代码实践

**实践目标**：同一个句柄先以 receiver 连接，再 `reconnect` 成 sender，观察 `recv_count()`、`mode()`、`valid()` 的变化。

**操作步骤**：

1. 按 u1-l2 构建出 `ipc` 库。
2. 新建文件 `demo_lifecycle.cpp`，链接 `ipc` 库：

```cpp
// 示例代码：演示 connect(receiver) → reconnect(sender) 的角色切换
#include "libipc/ipc.h"
#include <iostream>

int main() {
    ipc::channel ipc {"demo-switch", ipc::receiver};   // 先当接收者
    std::cout << "[receiver] valid=" << ipc.valid()    // 预期 1
              << " mode=" << ipc.mode()                // 预期 1 (receiver)
              << " recv_count=" << ipc.recv_count()    // 预期 1（自己是唯一接收者）
              << "\n";

    bool ok = ipc.reconnect(ipc::sender);              // 切换为发送者
    std::cout << "reconnect(sender) ok=" << ok << "\n";// 预期 1
    std::cout << "[sender]   valid=" << ipc.valid()    // 预期 1（句柄仍在）
              << " mode=" << ipc.mode()                // 预期 0 (sender)
              << " recv_count=" << ipc.recv_count()    // 预期 0（撤销了接收者位）
              << "\n";
    return 0;
}
```

3. 编译运行（路径按你本机构建结果调整）：`g++ -std=c++17 demo_lifecycle.cpp -I include -L lib -lipc -pthread -lrt && ./a.out`

**需要观察的现象**：

- 切换前后 `valid()` 始终为真（句柄没销毁，只是换了角色）。
- `mode()` 从 `1` 变 `0`。
- `recv_count()` 从 `1` 变 `0`——这直接证明了「角色切换 = 接收者位的撤销/建立」。

**预期结果**：见代码注释。`recv_count` 的具体值**待本地验证**（若此时另有别的进程以 receiver 连了同名通道，初始值会大于 1）。

#### 4.3.5 小练习与答案

**练习 1**：`mode & receiver` 当 `mode` 分别为 `ipc::sender`、`ipc::receiver`、`ipc::sender|ipc::receiver` 时，结果各是多少？分别代表什么？

> **答**：`0 & 1 = 0`（当发送者）；`1 & 1 = 1`（当接收者）；`1 & 1 = 1`（默认 connect 时当接收者）。三者中只要 receiver 位为 1，`start_to_recv` 就是 true。

**练习 2**：为什么 `reconnect(ipc::sender)` 之后 `recv_count()` 会从 1 变成 0，而不是「保持 1 但角色改变」？

> **答**：因为 `recv_count()` 数的是「接收者连接位」的个数。`reconnect` 切到发送者时会调用 `disconnect_receiver()` 撤销自己原本的接收位，所以计数减一；它并不会另外维护一个独立的「角色」字段去和连接位解耦。

---

### 4.4 release / clear / clear_storage：三档资源清理

#### 4.4.1 概念说明

libipc 把「回收资源」拆成了**四个粒度**，初学者最容易混淆。本模块就是把它们一次说清：

| 动作 | 触发方式 | 是否断连 | 是否清理共享内存文件 | 是否释放本地对象 | 典型用途 |
| --- | --- | --- | --- | --- | --- |
| 析构 `~chan_wrapper` | 离开作用域自动 | 是（优雅） | 否 | 是 | 正常 RAII 收尾 |
| `release()` | 手动 | **否** | 否 | 是 | 「我走了，别等我对端」 |
| `clear()` | 手动 | 是 | **是**（依赖项） | 是 | 强制回收该句柄依赖的共享内存 |
| `clear_storage(name)` | 静态方法 | 不需要句柄 | **是**（按名字） | — | 进程退出后按名字清掉残留共享内存 |

理解它们的差异，关键在于看 `chan_impl` 的实现里「先 disconnect 还是先 destroy」「调不调 `conn_info->clear()`」。

#### 4.4.2 核心流程

```text
~chan_wrapper / destroy :  disconnect(h) ──► $delete(h)           // 优雅断连 + 释放

release(h)              :  $delete(h)                             // 不断连，直接释放本地对象
                                                                  //   （对端的连接位会残留，靠共享内存引用计数兜底）

clear(h)                :  disconnect(h) ──► conn_info->clear() ──► destroy(h)
                                                                  //   clear() 强制释放 waiter/queue 的共享内存句柄

clear_storage(name)     :  waiter::clear_storage(...) + shm::clear_storage(...)
                                                                  //   按名字删除 QU/CC/WT/RD/AC_CONN__ 等共享内存对象
```

#### 4.4.3 源码精读

**`release`：不断连就释放**（[ipc.h:94-98](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L94-L98) + [ipc.cpp:784-787](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L784-L787)）：

```cpp
// ipc.h
void release() noexcept {
    detail_t::release(h_);
    h_ = nullptr;
}
// ipc.cpp —— 注意：直接调 destroy，没有 disconnect！
void chan_impl<Flag>::release(ipc::handle_t h) noexcept {
    detail_impl<policy_t<Flag>>::destroy(h);   // 仅 mem::$delete
}
```

对比 `chan_impl::destroy`（[ipc.cpp:778-782](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L778-L782)）会先 `disconnect(h)`。所以 `release` 的语义是「**释放本地内存，但不去通知对端、不撤销连接位**」。`release` 之后 `h_` 被置空，`valid()` 变假，对象不可再用。

**`clear`：强制清理该句柄依赖的共享内存**（[ipc.h:100-104](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L100-L104) + [ipc.cpp:795-803](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L795-L803)）：

```cpp
void chan_impl<Flag>::clear(ipc::handle_t h) noexcept {
    disconnect(h);
    auto conn_info_p = static_cast<conn_info_t *>(h);
    if (conn_info_p == nullptr) return;
    conn_info_p->clear();     // 释放 waiter 句柄 + queue 句柄（共享内存）
    destroy(h);               // 再断连 + 释放本地对象
}
```

`conn_info_t::clear()` 会逐个释放 `cc_waiter_/wt_waiter_/rd_waiter_` 与 `que_` 持有的共享内存句柄（见 [ipc.cpp:417-420](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L417-L420) 与 [145-150](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L145-L150)）。这是「强制」清理——连对端可能还在用的共享内存也一起释放（依赖引用计数保证安全）。

**`clear_storage(name)`：不需要句柄，按名字删残留**（[ipc.h:107-114](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/ipc.h#L107-L114) + [ipc.cpp:805-814](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L805-L814)）：

```cpp
void chan_impl<Flag>::clear_storage(prefix pref, char const * name) noexcept {
    conn_info_t::clear_storage(pref.str, name);
}
```

它最终调用 `conn_info_t::clear_storage`（[ipc.cpp:422-429](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L422-L429)），按命名规则删除 `QU_CONN__`、`CC_CONN__`、`WT_CONN__`、`RD_CONN__`、`AC_CONN__` 这一组共享内存对象。典型用途：程序异常退出后，下次启动前手动清掉上次的残留通道。

#### 4.4.4 代码实践

**实践目标**：通过「源码阅读」把四种清理的调用链对齐，理解它们的边界。

**操作步骤**：

1. 打开 [ipc.cpp:778-814](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L778-L814)，把 `destroy`、`release`、`clear`、`clear_storage` 四个函数的函数体并排看。
2. 给每个函数打勾：它是否调用了 `disconnect`？是否调用了 `conn_info->clear()`？是否调用了 `$delete`？
3. 回答：如果一个 receiver 进程 `release()` 后崩溃，对端 sender 调 `recv_count()` 还会不会把这个失效 receiver 算进去？

**需要观察的现象 / 预期结果**：

- `destroy`：disconnect ✔ + $delete ✔。
- `release`：disconnect ✘ + $delete ✔。
- `clear`：disconnect ✔ + `conn_info->clear()` ✔ + $delete ✔。
- `clear_storage`：不需要句柄，直接按名字删共享内存对象。
- 第 3 问：会算进去。因为 `release` 没有撤销连接位，所以**除非用 `clear` / `clear_storage` 或对端用 `force_push`，否则失效 receiver 的连接位会残留**。这正是发送方需要 `force_push`（见 u1-l4）来「踢掉」失效读者的原因之一。（完整机制见 U4 广播章节。）

#### 4.4.5 小练习与答案

**练习 1**：`release()` 之后还能继续 `send()` 吗？

> **答**：不能。`release()` 把 `h_` 置为 `nullptr`，`valid()` 为假；再调 `send()` 时 `queue_of(h)` 返回 `nullptr`，返回 `false` 并打印错误日志。

**练习 2**：程序正常结束时，你**需要**手动调用 `clear_storage` 吗？

> **答**：一般不需要。正常析构走 `destroy`（优雅断连 + 释放），共享内存的跨进程引用计数会在最后一个引用释放时回收。`clear_storage` 主要用于「上一次异常退出留下残留、本次启动想强制清干净」的运维场景。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「单进程句柄生命周期观察器」：

**任务**：写一个程序，按顺序演示一条 `ipc::channel` 句柄的完整生命周期，每一步打印 `valid()` / `mode()` / `recv_count()`，验证你对状态机的理解。

**建议步骤**：

1. 默认构造 → 打印状态（预期 valid=0）。
2. `connect("life", ipc::receiver)` → 打印状态（预期 valid=1, mode=1, recv_count=1）。
3. `clone()` 出第二个 receiver → 打印原句柄与克隆句柄的 `recv_count()`（预期都变成 2，因为多了个接收者）。
4. 对原句柄 `reconnect(ipc::sender)` → 打印状态（预期 mode=0, recv_count=1，因为还剩克隆的那个接收者）。
5. 克隆句柄析构 → 再打印原句柄 `recv_count()`（预期回到 0）。
6. 最后 `clear_storage("life")` 清理残留共享内存，确认程序能干净退出。

**思考题（结合源码）**：

- 第 3 步为什么两个句柄的 `recv_count()` 都是 2 而不是「一个 1 一个 1」？（提示：`recv_count` 数的是共享内存里连接位图的总位数，对同一通道的所有句柄一致。）
- 如果第 4 步把 `reconnect` 换成 `release()`，`recv_count()` 会变成多少？为什么？（提示：`release` 不撤销连接位。）

**预期结果**：各步数值见括号。具体数字**待本地验证**（需先构建库并编译运行）。

## 6. 本讲小结

- `chan_impl<Flag>` 是一张「按策略 `Flag` 分派的静态函数表」，配合 `void*` 句柄实现类型擦除；真正干活的是 `.cpp` 里的 `detail_impl<policy_t<Flag>>`。
- `chan_wrapper<Flag>` 才是用户类型，持有 `h_`（句柄）、`mode_`（角色）、`connected_` 三个成员，是标准的 RAII 类型；`chan`/`route`/`channel` 都是它的别名。
- 模式编码的关键是 `sender==0, receiver==1`，库用 `mode & receiver` 提取「receiver 位」作为 `start_to_recv`；注意构造函数默认 mode 是 `sender`，而 `connect()` 默认 mode 是 `sender|receiver`（=receiver）。
- `connect` 负责「建对象 + 连名」，`reconnect` 负责「在同一对象上切换角色」；切换会撤销/建立接收者位，直接反映在 `recv_count()` 上。
- 资源清理分四档：析构（优雅断连+释放）、`release`（不断连直接释放）、`clear`（强制清依赖的共享内存）、`clear_storage`（按名字删残留）。
- 显式实例化门（[ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)）决定只有 3 种 `wr` 组合真正可用，单播的多消费者版本标注为 `TBD`。

## 7. 下一步学习建议

本讲把「句柄这一层」讲透了，但句柄内部的 `conn_info_t`、`queue`、`waiter` 还是个黑盒。建议接下来：

- **u2-l4（route vs channel）**：用本讲学到的 `recv_count` / 连接位图知识，理解广播模式的 32 接收者限制是怎么来的。
- **U3（核心数据通路）**：打开 `ipc.cpp` 的 `detail_impl`，跟踪一条消息从 `send` 到 `recv` 真正经历了哪些函数、共享内存对象（`QU_CONN__`、`CC_CONN__` 等），把本讲提到的这些命名对象和真实代码对上号。
- **U6（同步原语）**：本讲反复提到的 `cc_waiter_` / `wt_waiter_` / `rd_waiter_`，其内部实现（condition + mutex）正是 U6 的主题。
