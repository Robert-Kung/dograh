/**
 * customer-center-platform fork 的標記常數（母 repo W2d task 2.0d）。
 *
 * 這個檔與這顆常數**不是**上游 dograh 的一部分。它存在的唯一理由是讓部署層
 * 能機器可判地回答「現在跑的這份 bundle 是不是我方 fork」——rebase 衝突解析
 * 時把唯讀覆蓋面靜默解掉、或誤把 `image:` 指回上游 prebuilt，兩種失效的形狀
 * 都是「按鈕全 enabled、無角色訊號」而**畫面看起來完全正常**。
 *
 * 讀者是母 repo 的 `deploy/check-runtime-consistency.sh`（RUNBOOK 步驟 7，
 * 位置在 `up` 之後），對執行中容器的 `.next/static` 斷言這個字面存在。
 *
 * 因此：
 * - 這個字面 **MUST NOT** 被拆成拼接式（`'ccp-' + 'fork-'`），否則 minify 後
 *   grep 不到；
 * - 它 **SHALL** 有一個真的會被渲染的使用點（見 `access.tsx` 的 `data-ccp-fork`），
 *   否則 tree-shaking 會把它整個拿掉而斷言恆假；
 * - 它偵測的是「跑的是不是我方 fork」。**降級到上一次成功建置的我方映像它偵測
 *   不到**（舊 fork 映像照樣含這個標記）——那一軸由 ui 映像的 OCI label
 *   （submodule SHA）承擔，見母 repo W2d task 1.2i④。
 */
export const CCP_FORK_MARKER = 'ccp-fork-w2d-readonly-v1';
