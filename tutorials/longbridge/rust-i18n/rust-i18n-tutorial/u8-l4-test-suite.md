# 测试体系与质量保障

## 1. 本讲目标

前面几讲我们逐一拆解了 rust-i18n 的运行时机制——回退链（[u3-l4](u3-l4-fallback-mechanism.md)）、全局 locale 的无锁并发（[u8-l1](u8-l1-atomicstr-thread-safety.md)）。本讲换一个视角：**项目用什么测试来保证这些机制不出错**。

读完本讲，你应当能够：

1. 看懂 `tests/` 下三份集成测试文件各自的作用，以及「每份文件顶部各调一次 `i18n!`」这一约定背后的原因。
2. 掌握 `integration_tests.rs` 用**子模块（submodule）隔离**多个 `i18n!` 配置、一次编译测多种 fallback 的技巧。
3. 说清为什么集成测试**必须在单线程**（`RUST_TEST_THREADS=1`）下运行，以及项目用什么手段把这个约束「焊死」。
4. 理解 `multi_threading.rs` 如何在单线程测试框架里、用**测试函数内部自建线程**的方式验证并发安全。
5. 读懂提取器 crate 用 `#[cfg(test)]` 内联单元测试 + `include_str!` + `build_messages!` 宏的测试组织方式。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：集成测试在 Rust 里就是 `tests/` 目录下的独立 crate。** Cargo 会把 `tests/` 下每一个 `.rs` 文件（不是 `mod`）编译成一个**独立的集成测试二进制**。这意味着：

- 每份测试文件就是一个 crate，拥有**自己独立的全局静态变量**。
- 回忆 [u3-l1](u3-l1-t-macro-call-chain.md)：`_RUST_I18N_BACKEND`（静态后端）和 `CURRENT_LOCALE`（全局 locale）都是进程级/crate 级的 `static`。所以**每份集成测试文件都必须在自己的顶部调一次 `i18n!(...)`**，否则该文件里用 `t!` 时会编译报错（找不到 `_rust_i18n_t`）。这解释了为什么本讲三份文件顶部都有一句 `i18n!`。

**直觉二：「全局状态」是测试的天然敌人。** `set_locale` 改的是全局 `CURRENT_LOCALE`，而很多测试的断言（比如 `t!("hello")` 不带 `locale=` 时）依赖当前全局 locale。如果两个测试**同时**跑，A 刚 `set_locale("en")`，B 立刻 `set_locale("zh-CN")`，A 随后的断言就会读到 B 设的值而失败。这就是集成测试必须串行（单线程）的根本原因。

**直觉三：测试框架的「线程数」和测试函数「自建的线程」是两回事。** `RUST_TEST_THREADS=1` 限制的是 **libtest 框架同时运行几个测试函数**，而不是测试函数内部能不能 `spawn` 线程。所以「整体单线程串行」和「某个测试内部狂开线程」并不矛盾——这正是 `multi_threading.rs` 能在单线程框架下验证并发安全的前提。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tests/integration_tests.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs) | 主集成测试：覆盖回退、插值、多格式、自定义后端；用子模块测多种 `i18n!` 配置；含单线程环境断言 |
| [tests/multi_threading.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs) | 并发安全测试：一写多读，验证 `set_locale`/`t!` 在高频并发下不崩溃、不读到半截字符串 |
| [tests/i18n_minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs) | 短键（minify_key）专属集成测试：验证 `_RUST_I18N_MINIFY_KEY_*` 系列常量与 `tkv!` 输出 |
| [crates/extract/src/extractor.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs) | 提取器库的源码，文件末尾带 `#[cfg(test)] mod tests` 内联单元测试 |
| [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml) | 测试相关配置：`[dev-dependencies]` 的 codegen feature、`exclude`、`[[bench]] harness` |
| [.cargo/config.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/.cargo/config.toml) | 全局强制 `RUST_TEST_THREADS = "1"` 的「焊死」配置 |

## 4. 核心概念与源码讲解

### 4.1 集成测试布局：三份文件、各自初始化、子模块隔离

#### 4.1.1 概念说明

rust-i18n 的集成测试放在 `tests/` 目录下，共三份 `.rs` 文件。Cargo 把它们各自编译成独立 crate，因此每份都**必须在顶层调一次 `i18n!`** 来生成自己的静态后端与 `_rust_i18n_translate` 入口（见 [u2-l4](u2-l4-generate-code.md)）。三份文件的分工是：

- `integration_tests.rs`：**主力**，覆盖 fallback、变量插值、`=>`/`=` 两种参数写法、JSON/TOML/YAML 多格式合并、自定义后端 `TestBackend`、嵌套 locale 文件（v2）等。
- `multi_threading.rs`：**并发安全**专项。
- `i18n_minify_key.rs`：**短键**专项，验证 minify_key 相关常量与 `tkv!` 元组输出（见 [u6-l2](u6-l2-minify-key-macros.md)）。

> 为什么不把短键、多线程测试都塞进 `integration_tests.rs`？因为它们各自需要**不同的 `i18n!` 配置**。比如短键测试需要 `minify_key = true, minify_key_prefix = "t_"`，而主测试不需要。同一份文件里，顶层的 `i18n!` 只能有一份「全局默认」配置；要测别的配置，就得开新的测试目标（新文件）或在子模块里再调 `i18n!`（见 4.2）。

#### 4.1.2 核心流程

一份集成测试文件的骨架是：

```text
1. 顶层 use 引入需要的类型/宏
2. 顶层调一次 i18n!(...) —— 生成「默认配置」的后端与 _rust_i18n_translate
3. #[cfg(test)] mod tests {
       自由地写 #[test] fn ...
       （可选）再用子模块 i18n! 测别的配置
   }
```

#### 4.1.3 源码精读

`integration_tests.rs` 顶层的初始化语句，注意它同时用了 `fallback` 和自定义 `backend`：

[tests/integration_tests.rs:41-45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L41-L45) —— 顶层 `i18n!` 加载 `./tests/locales`、设回退为 `en`、并接入自定义 `TestBackend`，使本文件所有不带前缀的 `t!` 都用这份配置。

这份顶层配置自带的 `TestBackend` 是一个手写的 `Backend` 实现，演示了「自定义后端优先于本地文件」（见 [u4-l3](u4-l3-custom-backend.md)）：

[tests/integration_tests.rs:3-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L3-L39) —— `TestBackend` 只在 `pt` locale 下返回一个假翻译 `foo`，`translate` 命中返回 `Some`、未命中返回 `None`，**绝不自写回退逻辑**（回退由 `_rust_i18n_try_translate` 编排，见 [u3-l4](u3-l4-fallback-mechanism.md)）。

对应断言在文件末尾：

[tests/integration_tests.rs:285-288](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L285-L288) —— `t!("foo", locale = "pt")` 命中 `TestBackend` 的假值 `"pt-fake.foo"`，验证自定义后端确实被接入并优先。

短键测试文件 `i18n_minify_key.rs` 的顶层 `i18n!` 则是一组截然不同的配置：

[tests/i18n_minify_key.rs:1-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L1-L8) —— 开启 `minify_key = true`，设 `len=24`、`prefix="t_"`、`thresh=4`，专门用于验证短键行为；这与主集成测试完全隔离，互不干扰。

#### 4.1.4 代码实践

**实践目标**：体会「一份文件 = 一个 crate = 一份顶层 `i18n!`」的约定。

**操作步骤**：

1. 打开 `tests/` 目录，确认有且仅有三份 `.rs` 文件，每份顶部都有 `rust_i18n::i18n!(...)`。
2. 临时把 `tests/multi_threading.rs` 第 6 行的 `rust_i18n::i18n!("locales", fallback = "en");` 注释掉。
3. 运行 `RUST_TEST_THREADS=1 cargo test --test multi_threading`。

**需要观察的现象**：编译报错，提示找不到 `_rust_i18n_t`（或类似符号）。

**预期结果**：因为该文件不再有自己的静态后端与内部宏，`t!` 转发壳无处可转（见 [u3-l1](u3-l1-t-macro-call-chain.md) 第一跳）。还原注释即可恢复。这印证了「每份集成测试文件必须自己初始化」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `i18n_minify_key.rs` 不能合并进 `integration_tests.rs`？

> **参考答案**：因为短键测试需要 `minify_key = true` 等一套特定配置，而 `integration_tests.rs` 的顶层 `i18n!` 已经固定为另一套（不带短键）。同一份测试文件里，顶层只能有一个 `i18n!` 作为「默认配置」；要换配置，要么开新测试目标（新文件），要么在子模块里再调一次 `i18n!`（见 4.2）。把它们拆成两份文件是最干净的隔离方式。

**练习 2**：`tests/locales/` 目录下的 `v2.yml` 文件里有一堆 `t_xxxxx` 开头的短键，它们是给哪份测试用的？

> **参考答案**：这些短键是 `i18n_minify_key.rs` 的测试数据。`i18n_minify_key.rs` 顶层 `i18n!` 加载 `./tests/locales` 时会一并读取 `v2.yml`，里面预置了若干「长文案 → 短键」的对照，用于断言短键算法输出（见 4.1.3 的 `tkv!` 断言）。

---

### 4.2 子模块隔离技巧：一个 crate 内测多种 fallback 配置

#### 4.2.1 概念说明

`i18n!` 生成的 `_RUST_I18N_BACKEND` 和 `_rust_i18n_translate` 等符号，其**可见性与所属路径**取决于 `i18n!` 被调用的位置。如果在不同 `mod` 里各调一次 `i18n!`，就会生成**各自独立、互不干扰**的一组后端/查找函数，路径分别是 `crate::tests::test1::_rust_i18n_translate`、`crate::tests::test2::_rust_i18n_translate`……

`integration_tests.rs` 正是利用这一点，在**同一份测试文件、同一次编译**里，用 `test0`~`test5` 六个子模块测了六种不同的 `i18n!` 配置（主要是不同的 `fallback`）。这比「为每种 fallback 开一份测试文件」要省事得多，也更容易横向对比。

#### 4.2.2 核心流程

```text
mod tests {
    mod test1 {
        rust_i18n::i18n!("./tests/locales", fallback = "en");
        // 生成 crate::tests::test1::_rust_i18n_translate
        // 该模块自己的 _RUST_I18N_BACKEND、_RUST_I18N_FALLBACK_LOCALE
        #[test] fn test_fallback() {
            // 用全路径 crate::tests::test1::_rust_i18n_translate(...) 精确指定用哪份后端
        }
    }
    mod test2 { ... fallback = "zh-CN" ... }   // 又一套独立后端
    mod test4 { ... fallback = ["zh", "en"] ... }  // 数组形态 fallback
}
```

关键点：调用时必须写**全路径**（`crate::tests::test1::_rust_i18n_translate(...)`）来明确「我要用的是 test1 这份后端」，否则会引用到外层默认那份。

#### 4.2.3 源码精读

测**单字符串 fallback** 的子模块，用全路径调用对应的查找函数：

[tests/integration_tests.rs:56-66](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L56-L66) —— `test1` 用 `fallback = "en"` 初始化；`test_fallback` 断言查不到 `missing.default` 时回退到 `en` 的译文。注意调用的是 `crate::tests::test1::_rust_i18n_translate`，精确指向 test1 这份后端。

测**数组形态 fallback**（见 [u2-l1](u2-l1-i18n-macro-arg-parsing.md) 的 fallback 双形态解析）的子模块：

[tests/integration_tests.rs:84-98](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L84-L98) —— `test4` 用 `fallback = ["zh", "en"]`；它同时验证「`zh` 命中」与「`zh` 没有、回退到 `en` 命中」两种情况，正好覆盖显式 fallback 列表的顺序短路逻辑（[u3-l4](u3-l4-fallback-mechanism.md)）。

此外还有一些**不带 `#[test]` 的空子模块**（如 `test0`、`test3`、`test5`），它们只为「确认这种 `i18n!` 调用形式能编译通过」而存在：

[tests/integration_tests.rs:80-102](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L80-L102) —— `test3` 测 `i18n!(fallback = "foo")`（不传路径）、`test5` 测 `i18n!()`（全默认）能否编译，这是「编译期冒烟测试」。

#### 4.2.4 代码实践

**实践目标**：学会用「子模块 + 全路径调用」横向对比同一键在不同 fallback 配置下的结果。

**操作步骤**：

1. 阅读 `integration_tests.rs` 的 `test1`、`test2`、`test4` 三个子模块的 `i18n!` 配置。
2. 在纸上推导：对同一个键 `"missing.default"`、locale `"zh-CN"`：
   - test1（`fallback = "en"`）会回退到 `en`；
   - test4（`fallback = ["zh","en"]`）会先试 `zh` 再试 `en`。
3. 用 `RUST_TEST_THREADS=1 cargo test --test integration_tests test_fallback` 观察这些断言全部通过。

**需要观察的现象**：三种子模块的 `test_fallback` 互不影响地全部通过。

**预期结果**：因为每个子模块有自己的 `_RUST_I18N_FALLBACK_LOCALE` 静态量，fallback 配置彼此隔离；这正是子模块技巧的价值。**待本地验证**：如果你把 `test1` 的断言改成调用 `crate::tests::test4::_rust_i18n_translate(...)`（用错了路径），会读到 test4 的 fallback 配置，断言可能失败。

#### 4.2.5 小练习与答案

**练习**：子模块 `test1` 里的 `#[test] fn test_fallback` 为什么必须写成 `crate::tests::test1::_rust_i18n_translate(...)`，而不能直接写 `_rust_i18n_translate(...)`？

> **参考答案**：因为本文件**顶层**（第 41 行）已经调过一次 `i18n!`，它在 `crate` 根生成了 `_rust_i18n_translate`；而 `test1` 子模块内部又调了一次 `i18n!`，在 `crate::tests::test1` 路径下生成了**另一份**。直接写 `_rust_i18n_translate` 会引用到外层顶层那份（fallback=en，但带 TestBackend），而不是 test1 这份。写全路径才能精确选定要测的那份后端，这正是子模块隔离测试的前提。

---

### 4.3 单线程约束：`RUST_TEST_THREADS=1` 的根因与两层强制

#### 4.3.1 概念说明

这是本讲最核心、也最容易被误解的一点。rust-i18n 的集成测试**必须单线程串行**运行，根因只有一个：**`CURRENT_LOCALE` 是进程级共享的全局状态**（见 [u8-l1](u8-l1-atomicstr-thread-safety.md)）。

- 很多测试调 `rust_i18n::set_locale("en")` / `set_locale("zh-CN")` 来切换全局 locale；
- 紧接着用**不带 `locale=`** 的 `t!("hello")` 取值，其结果依赖刚刚设置的全局 locale；
- 如果两个测试**并行**跑，B 的 `set_locale` 会随时改掉 A 依赖的全局值，导致 A 的断言时过时不过、flaky 失败。

项目用**两层手段**把这个约束落实：

1. **配置层**：`.cargo/config.toml` 里 `[env] RUST_TEST_THREADS = "1"`，对所有 `cargo test` 全局生效。
2. **运行时断言层**：`integration_tests.rs` 里有一个 `check_test_environment` 测试，**主动检查**环境变量确实为 `"1"`，否则该测试失败并给出提示。

#### 4.3.2 核心流程

```text
cargo test
  └─ Cargo 读取 .cargo/config.toml → 注入 RUST_TEST_THREADS=1
       └─ libtest 框架据此「同一时刻只跑一个 #[test]」
            └─ check_test_environment 额外断言 env==“1”，作为一道兜底
```

两层是互补的：配置层是「默认正确」，断言层是「防止有人用 `--test-threads=4` 或旧版 cargo 绕过配置」。

#### 4.3.3 源码精读

**第一层：配置焊死。**

[.cargo/config.toml:1-2](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/.cargo/config.toml#L1-L2) —— `[env]` 段把 `RUST_TEST_THREADS` 固定为 `"1"`。Cargo 在执行任何测试前都会把这个变量注入子进程环境，libtest 据此只开一个工作线程，测试函数被**串行**调度。

**第二层：运行时兜底断言。**

[tests/integration_tests.rs:104-113](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L104-L113) —— `check_test_environment` 读取 `RUST_TEST_THREADS`，断言它等于 `"1"`，并在失败信息里解释原因（全局 locale 是共享状态），还提示旧版 cargo 用户该如何处理。

**为什么需要第二层？** 因为有人可能显式传 `cargo test -- --test-threads=4`，命令行参数会覆盖环境变量；或者用极旧的 cargo（< 1.56，那时环境变量机制尚未就绪）。这条断言把这些「绕过」都拦住，并在 CI 里立刻红出来。

**真实代价**：正因为强制单线程，凡是改了全局 locale 的测试都必须**自给自足**——自己设好 locale、自己断言、不指望别的测试「顺手」帮它复位。看一个典型例子：

[tests/integration_tests.rs:136-160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L136-L160) —— `test_t` 一上来就 `set_locale("en")`，中途又 `set_locale("zh-CN")`、再 `set_locale("en")`。每个测试函数都**显式设置**自己依赖的全局 locale，绝不依赖前一个测试留下的状态——这是在单线程串行约定下的正确写法。即便如此，若有人改成并行，这些 `set_locale` 仍会互相践踏。

> 旁证：本仓库当前 HEAD 提交 `97cf091`（PR #141）就是为了修测试基础设施——它给 `[dev-dependencies]` 的 `rust-i18n-support` 加上 `features = ["codegen"]`，因为集成测试里 `use rust_i18n_support::load_locales;`（[tests/integration_tests.rs:50](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L50)）而 `load_locales` 被代码生成 feature 守卫（见 [u5-l3](u5-l3-feature-flags.md)）。对应配置：

[Cargo.toml:60-65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L60-L65) —— `[dev-dependencies]` 里给 `rust-i18n-support` 显式开启 `codegen` feature，使测试能直接调用 `load_locales` 验证文件加载（见 `test_load`，[tests/integration_tests.rs:115-118](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L115-L118)）。这说明测试体系本身也需要 feature/依赖的正确配套。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「并行运行会让依赖全局 locale 的测试 flaky」。

**操作步骤**：

1. **第一次**（正确）：`RUST_TEST_THREADS=1 cargo test --test integration_tests`，确认全绿。
2. **第二次**（故意破坏约束）：`cargo test --test integration_tests -- --test-threads=4`，多跑几次。

**需要观察的现象**：第一次稳定通过；第二次可能出现间歇性失败（具体哪些用例失败、是否每次都失败，取决于线程调度，**待本地验证**）。

**预期结果**：失败的根源是 `test_t`、`test_t_with_locale_and_args` 等用例里对 `set_locale` 的依赖被并发 `set_locale` 践踏。同时 `check_test_environment` 会**稳定失败**（因为它直接断言 `env == "1"`），这是项目刻意设的「红线」，提醒你跑错了。

#### 4.3.5 小练习与答案

**练习 1**：如果有人删掉 `.cargo/config.toml`，只保留 `check_test_environment` 断言，默认 `cargo test` 还会单线程吗？

> **参考答案**：不会。`check_test_environment` 只在测试**运行时**报错，无法改变 libtest 的线程数。删掉配置后，libtest 默认按 CPU 核数并行；结果是大量测试 flaky 失败，**外加** `check_test_environment` 本身失败。配置层是「让默认行为就正确」，断言层只是「出错时给个清晰提示」，二者必须配合。

**练习 2**：为什么 `t!("hello", locale = "zh-CN")`（带显式 locale）的测试**相对**不怕并行，而 `set_locale("zh-CN"); t!("hello")`（依赖全局）的测试很怕并行？

> **参考答案**：带显式 `locale=` 的 `t!` 不读取全局 `CURRENT_LOCALE`，locale 参数在编译期就被 `_tr!` 提取为查找参数（见 [u3-l2](u3-l2-tr-macro-codegen.md) 的 `filter_arguments`），与全局状态无关，因此并行安全。而不带 `locale=` 的 `t!` 在运行时以 `&rust_i18n::locale()` 作为默认 locale（见 [u8-l1](u8-l1-atomicstr-thread-safety.md)），其结果取决于「此刻」的全局值——一旦别的测试并发 `set_locale`，结果就不可预测。

---

### 4.4 多线程并发测试：`multi_threading.rs` 的设计

#### 4.4.1 概念说明

第 4.3 节说集成测试要单线程，但 rust-i18n 的运行时**承诺**多线程安全（`AtomicStr` + arc-swap 无锁，见 [u8-l1](u8-l1-atomicstr-thread-safety.md)）。这个承诺怎么测？

答案在 `multi_threading.rs`：它在**单个测试函数内部**用 `std::thread::spawn` 自建线程，模拟「一个线程高频 `set_locale`、多个线程高频 `t!`」的场景。注意这里的层次关系：

- libtest 框架层面：**单线程**（同时只跑一个 `#[test]` fn），由 `RUST_TEST_THREADS=1` 保证；
- `test_t_concurrent` 函数内部：**自己 spawn 了 1 + 4 个 OS 线程**并发跑。

这两层互不冲突——框架限制的是「测试函数之间」的并发，不限制「测试函数内部」的并发。

#### 4.4.2 核心流程

`test_t_concurrent` 的设计（伪代码）：

```text
end = now + 3s
线程 store:  循环 set_locale("en-N") / set_locale("fr-N")   // 疯狂改全局 locale
线程 load×4: 循环 t!("hello") 和 t!("hello", locale=...)    // 疯狂读
join 所有线程
断言：整个过程没有 panic、没有 join 出错
```

关键点：**这类并发测试不断言「译文具体是什么」**，只断言「不崩溃、不读到半截字符串导致 panic」。因为在并发改 locale 下，任何具体的译文断言都会 flaky；正确性由单元测试（固定 locale）保证，并发测试只保证「线程安全 / 不会撕裂读取」。

#### 4.4.3 源码精读

文件顶部的初始化（fallback 为 `en`）：

[tests/multi_threading.rs:1-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L1-L6) —— 引入 `set_locale`/`t!` 与线程相关 API，并调 `i18n!("locales", fallback = "en")` 生成本文件的静态后端。

「一写一读」的基础并发测试，持续 3 秒：

[tests/multi_threading.rs:8-33](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L8-L33) —— `store` 线程反复 `set_locale("en-N")`/`set_locale("fr-N")`（用 `wrapping_add` 造大量不同 locale），`load` 线程反复 `t!("hello")`；最后 `join().unwrap()` 断言两线程都正常结束。若 `locale()` 返回过悬垂引用或半截字符串，这里会 panic 或崩溃。

「一写四读」的高压并发测试，且读线程混合使用全局 locale 与显式 locale：

[tests/multi_threading.rs:35-73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/multi_threading.rs#L35-L73) —— 4 个读线程用 `available_locales!()` 枚举 locale，按 `i % num_locales` 决定走全局还是显式 locale；`t!("hello")` 与 `t!("hello", locale=&locales[m])` 混跑。这同时压测了「全局 locale 的无锁读」与「`available_locales!()` 的读取」，验证 [u8-l1](u8-l1-atomicstr-thread-safety.md) 中 `AtomicStr`/arc-swap 的 RCU 无锁设计在真实并发下不撕裂。

> 为什么读线程不断言译文内容？因为 `store` 在不停把 locale 改成 `"en-123"`、`"fr-456"` 这类**根本不存在**的 locale，`t!` 大概率走 fallback 或返回 key 本身，值在不断变化，任何固定断言都会 flaky。并发测试的职责是「证明不崩」，而非「证明值对」。

#### 4.4.4 代码实践

**实践目标**：理解并发测试「只断言不崩」的设计，并亲手跑一次。

**操作步骤**：

1. 运行 `RUST_TEST_THREADS=1 cargo test --test multi_threading -- --nocapture`。
2. 观察它跑约 3 秒（两个测试函数各跑 3 秒）后通过。
3. 阅读 `test_t_concurrent`，注意读线程里 `t!("hello")`（全局 locale）与 `t!("hello", locale=...)`（显式）混用。

**需要观察的现象**：测试稳定通过、无 panic；耗时约 6 秒（两个 3 秒的测试串行）。

**预期结果**：这正是 arc-swap 无锁设计的实证——写端原子换指针、读端原子加载指针，`locale()` 返回的 `Guard` 守卫维持引用计数，故读端永远拿到完整字符串。如果你**好奇地**把 `RUST_TEST_THREADS` 调大重跑，这两个测试本身通常仍能通过（它们内部自建线程、不与别的测试争全局状态），但 `integration_tests` 会出问题——这恰好说明「并发测试自建线程」与「测试间单线程」是两件事。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `multi_threading.rs` 不断言 `t!("hello")` 的具体返回值？

> **参考答案**：因为 `store` 线程在不停 `set_locale("en-123")`、`"fr-456"` 等不存在的 locale，全局 locale 在飞速变化，`t!("hello")` 的返回值也随之变化（走 fallback 或返回 key）。固定断言必然 flaky。并发测试的职责是验证「线程安全、不撕裂、不 panic」，具体值的正确性由固定 locale 的单元/集成测试负责。

**练习 2**：`multi_threading.rs` 里并发线程那么多，为什么它仍然遵守 `RUST_TEST_THREADS=1`？

> **参考答案**：`RUST_TEST_THREADS=1` 限制的是 libtest 框架**同时运行几个 `#[test]` 函数**（这里是 1 个），而不是测试函数内部能否 `spawn` OS 线程。`test_t_concurrent` 在自己内部 spawn 线程是允许的；框架保证「同一时刻只有这一个测试函数在跑」，从而它对全局 `CURRENT_LOCALE` 的践踏不会干扰别的测试函数。

---

### 4.5 提取器内联 `#[cfg(test)]` 单元测试

#### 4.5.1 概念说明

前四节讲的都是 `tests/` 下的**集成测试**（黑盒，把 crate 当外部依赖）。本节讲另一种组织方式：**内联单元测试**，即把 `#[cfg(test)] mod tests { ... }` 直接写在源码文件末尾。rust-i18n 的提取器库 `rust-i18n-extract` 就这么做。

内联单元测试的特点：

- 与被测代码**同 crate**，能访问私有项（如 `format_message_key`、`Extractor` 结构体）。
- 用 `#[cfg(test)]` 守卫，编译产物里**完全不包含**测试代码，零体积代价。
- 适合测**纯函数**和小范围逻辑（如 `format_message_key` 的空白归一化、`extract` 的 token 提取）。

提取器（见 [u7-l2](u7-l2-source-iter-extract.md)）的核心是把源码里的 `t!(...)`/`tr!(...)` 调用提取成 `Message`。它的单元测试用了一个巧妙手法：把一段**示例源码**放进 `example.test.rs`，用 `include_str!` 读进来当输入，再断言提取结果。

#### 4.5.2 核心流程

```text
1. include_str!("example.test.rs") 读入一段「假装的源码」（含若干 t! 调用）
2. proc_macro2::TokenStream::from_str 把它变成 token 流
3. Extractor { ... }.invoke(stream) 执行提取，结果写进 results: HashMap
4. 用 build_messages! 宏手搓「期望结果」（含 key 与行号）
5. 按 index 排序后逐条 assert_eq! 比对
```

#### 4.5.3 源码精读

提取器源码的核心数据结构与提取入口（供理解被测对象）：

[crates/extract/src/extractor.rs:16-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L16-L22) —— `Message` 结构体含 `key`（译文内容）、`index`（首次出现序号）、`minify_key`、`locations`（多个出现位置）。

[crates/extract/src/extractor.rs:60-87](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L60-L87) —— `invoke` 递归扫描 token 流，用 `Ident + "!"` 模式识别宏调用，名字须在白名单 `METHOD_NAMES = ["t","tr"]`（[第 35 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L35)）中才会 `take_message`。

被测的纯函数 `format_message_key`（把连续空白归一化为单空格，使「文字相同、空白不同」的调用去重成一条 Message）：

[crates/extract/src/extractor.rs:147-151](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L147-L151) —— 用正则把 `\s+` 替换成单空格再 `trim`，是去重逻辑的关键。

`#[cfg(test)]` 内联测试模块，含一个 `build_messages!` 宏用来手搓期望结果：

[crates/extract/src/extractor.rs:153-180](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L153-L180) —— `#[cfg(test)] mod tests` 开头，`build_messages!` 宏把 `((key, line1, line2), ...)` 形式的输入展开成 `Vec<Message>`，免去重复样板代码。这是内联测试里常见的「用宏减少样板」手法。

针对纯函数的单元测试（不需要任何全局状态，天然并发安全）：

[crates/extract/src/extractor.rs:182-214](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L182-L214) —— `test_format_message_key` 用十几个 `assert_eq!` 覆盖 `format_message_key` 的各种空白/换行输入，验证归一化与去重逻辑。

针对完整提取流程的测试，用 `include_str!` 读示例源码：

[crates/extract/src/extractor.rs:216-257](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L216-L257) —— `test_extract` 把 `example.test.rs`（一段含若干 `t!` 调用的伪源码）喂给 `Extractor::invoke`，断言提取出的 `Message` 列表（含 key 与行号）与期望一致。注意它把 `index` 临时置 0 再比对——因为 `index` 取决于 `HashMap` 迭代顺序，不稳定，故排除后再比。

被测的示例源码本身（一段「假装的业务代码」，含字面量、注释、`r##"..."##` 原始字符串）：

[crates/extract/src/example.test.rs:1-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs#L1-L22) —— 里面故意放了多空格重复（第 18、20 行）来验证 `format_message_key` 的去重；第 11、14 行的原始字符串仅换行位置不同，归一化后应合并为同一条 Message、收集两个 Location。

#### 4.5.4 代码实践

**实践目标**：体会内联单元测试「同 crate 访问私有项 + `include_str!` 喂输入」的手法。

**操作步骤**：

1. 打开 `crates/extract/src/example.test.rs`，数一下里面有几处 `t!(...)` 调用（注意原始字符串那两处算同一条）。
2. 对照 [crates/extract/src/extractor.rs:221-235](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L221-L235) 的 `expected`，确认每条期望的 key 与行号都对得上。
3. 运行 `cargo test -p rust-i18n-extract`。

**需要观察的现象**：提取器测试通过；`example.test.rs` 里的注释（如 `// comment 1`）不会出现在提取结果里。

**预期结果**：因为 `extract` 先用 `syn::parse_file` 解析源码（[第 46 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L46)），注释在解析阶段就被剔除；且 `take_message` 只取宏**首参字面量**（[第 89-96 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L89-L96)），非字面量直接放弃。多空格的两条被 `format_message_key` 归并成一条、收集两个行号。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `test_extract` 在比对前要把 `actually_message.index = 0`（[第 252-253 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L252-L253)）？

> **参考答案**：`Message.index` 取自 `self.results.len()`（[第 121 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L121)），即「该 Message 是第几个被插入的」。但 `HashMap` 的迭代顺序不确定，`build_messages!` 手搓的期望里 `index` 都填 0。若不归零，两边 `index` 大概率不等而误报失败。归零后再比，等于「只比 key 和 locations，忽略插入顺序」。

**练习 2**：内联单元测试（`#[cfg(test)]`）相比集成测试（`tests/`），最大的优势是什么？

> **参考答案**：同 crate 可访问**私有项**（如本例的 `format_message_key`、`Extractor`、`build_messages!`），适合细粒度测试纯函数；且 `#[cfg(test)]` 保证测试代码**不进入发布产物**。缺点是无法测试「以外部用户视角调用公开 API」的端到端场景——那是 `tests/` 集成测试的职责。两者互补。

---

## 5. 综合实践

把本讲的知识串起来：**为 `t!` 的「带格式说明符的插值」补一个集成测试用例**，并说清它为什么必须单线程跑。

### 背景

[u3-l3](u3-l3-interpolation-and-format.md) 讲过，`t!("key", count = 42 : {:05})` 里的 `: {:05}` 会被 `_tr!` 包成 `format!("{:05}", 42)` = `"00042"`，再填入译文 `%{count}` 占位符。现有测试 `test_t_with_tt_val`（[tests/integration_tests.rs:162-181](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L162-L181)）测了 `count = 100`、`count = 1 + 2` 等，但**没有**测零填充格式说明符。我们来补上。

### 实践目标

新增一个测试，验证 `t!("messages.other", locale = "en", count = 42 : {:05})` 返回 `"You have 00042 messages."`，并解释单线程约束。

### 操作步骤（示例代码，请勿真的修改源码仓库文件，除非你在自己的 fork 里操作）

在 `tests/integration_tests.rs` 的 `mod tests` 内（与 `test_t_with_tt_val` 同级）新增以下测试函数。这是**示例代码**：

```rust
#[test]
fn test_t_with_format_specifier() {
    // 复用 en.yml 里已有的 messages.other: "You have %{count} messages."
    // : {:05} 让 count 被零填充成 5 位
    assert_eq!(
        t!("messages.other", locale = "en", count = 42 : {:05}),
        "You have 00042 messages."
    );
    // 负数同样受格式说明符影响
    assert_eq!(
        t!("messages.other", locale = "en", count = -7 : {:05}),
        "You have -0007 messages."
    );
}
```

然后运行：

```bash
RUST_TEST_THREADS=1 cargo test --test integration_tests test_t_with_format_specifier
```

### 需要观察的现象

测试通过；两条断言分别命中 `"You have 00042 messages."` 与 `"You have -0007 messages."`。

### 预期结果

- `en.yml` 里 `messages.other` 是 `"You have %{count} messages."`，`%{count}` 被 `replace_patterns` 替换成 `format!("{:05}", 42)` = `"00042"`。
- 即便不调用 `set_locale`（本例用显式 `locale = "en"`），它仍须在 `RUST_TEST_THREADS=1` 下运行，原因见下。

### 为什么必须单线程

1. **本测试虽用显式 `locale=`、不直接依赖全局 locale，但它与同一文件里大量依赖 `set_locale` 的测试共享一个 `CURRENT_LOCALE` 全局变量。** 例如 `test_t`（[第 136-160 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L136-L160)）会 `set_locale("zh-CN")`。若并行运行，整个 `integration_tests` 目标内的全局 locale 会被任意测试随时改写。
2. 更要紧的是 **`check_test_environment` 这道红线**：它断言 `RUST_TEST_THREADS == "1"`。只要并行跑（`--test-threads>1` 或删了 `.cargo/config.toml`），这个断言就会失败，CI 立刻红——这是项目刻意设的「提醒你跑错了」的机制。
3. 因此，**凡是放进 `tests/` 集成测试目录的用例，无论自身是否碰全局状态，都必须遵守 `RUST_TEST_THREADS=1`**，因为它们与同目录其他测试共享 crate 级全局变量、且受 `check_test_environment` 统一约束。这正是 [.cargo/config.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/.cargo/config.toml#L1-L2) 全局焊死单线程、再用 [check_test_environment](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L104-L113) 兜底的原因。

> 想测「格式说明符本身」而不受全局 locale 干扰，更稳妥的做法是放进**独立子模块**（仿 4.2 的 `test1`）并在断言里用全路径调用，或干脆开一份新测试文件。本综合实践用最简单的「加一个 `#[test]` fn」是为了演示流程。

## 6. 本讲小结

- rust-i18n 的集成测试分三份文件（`integration_tests`、`multi_threading`、`i18n_minify_key`），每份是独立 crate，**各自顶部调一次 `i18n!`** 生成自己的静态后端，互不干扰。
- `integration_tests.rs` 用**子模块隔离**（`test0`~`test5`）在一次编译里测多种 fallback 配置，调用时用**全路径** `crate::tests::testN::_rust_i18n_translate(...)` 精确选定后端。
- 集成测试**必须 `RUST_TEST_THREADS=1`**，根因是 `CURRENT_LOCALE` 为进程级共享全局状态，并行 `set_locale` 会互相践踏；项目用**两层**强制：`.cargo/config.toml` 全局注入 + `check_test_environment` 运行时断言兜底。
- `multi_threading.rs` 在单线程框架内**自建 OS 线程**做「一写多读」高压并发，只断言「不崩溃、不撕裂」，不断言具体译文（值在并发下不可预测）。
- 提取器库用 `#[cfg(test)] mod tests` **内联单元测试**，借 `include_str!("example.test.rs")` 喂示例源码、用 `build_messages!` 宏减少样板，能访问私有纯函数如 `format_message_key`。
- 「框架线程数」与「测试函数内部线程数」是两件事：前者由 `RUST_TEST_THREADS` 控制测试函数间的并发，后者不受限——这正是「整体单线程」与「内部并发测试」能共存的前提。

## 7. 下一步学习建议

本讲是「并发、性能与工程实践」单元的收尾。建议：

- **回顾本单元**：把 [u8-l1](u8-l1-atomicstr-thread-safety.md)（`AtomicStr` 无锁机制）与本讲的 `multi_threading.rs` 对照读，理解「机制」与「验证机制的测试」如何对应。
- **自己动手扩展测试**：仿照本讲综合实践，为多级 territory fallback（`zh-Hant-CN` → `zh-Hant` → `zh`，见 [u3-l4](u3-l4-fallback-mechanism.md)）补一个用子模块隔离的集成测试，亲历「配置隔离 + 全路径调用 + 单线程」三件套。
- **通读 CI 与构建**：结合 [u5-l2](u5-l2-build-script.md)（build.rs 增量重编译）与 [.github/workflows/ci.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/.github/workflows/ci.yml)，理解从「改一行 yml」到「CI 跑 `make test` 验证」的完整质量闭环。
- **如需二次开发**：实现自定义 `Backend` 时，参考 `integration_tests.rs` 顶层的 `TestBackend`（[第 3-39 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L3-L39)）作为最小可测样板，并用同样的「子模块 + 全路径」方式为它写隔离测试。
