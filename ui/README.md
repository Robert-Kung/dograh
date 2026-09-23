This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

### Login Flow

1. The redirection happens server side using `ui/src/stack.tsx` after the user has logged in.

### Sentry and PostHog

1. Initialized in `ui/src/instrumentation-client.ts`

## customer-center-platform fork：重生成 API client（母 repo W3b task 3.3）

`src/client/types.gen.ts`／`sdk.gen.ts` 由 `@hey-api/openapi-ts` 產生。上游 config
（`openapi-ts.config.ts`）的輸入是 dev server；本 fork **以檔案輸入**重生成，config 不改：

```bash
# 自 ui/ 執行；主機 Node 18 跑不動 hey-api 0.95，用與 Dockerfile 同大版的容器
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/..:/dograh" -w /dograh/ui \
  node:20-alpine sh -c 'npx openapi-ts -i ../docs/api-reference/openapi.json'
```

**重生成後的審計是程序義務**（不是自動守衛——再漂移無 CI 守衛，登記於母 repo RESIDUAL-RISKS）：

1. `git diff --stat src/client`；`types.gen.ts` 零 runtime 匯出，可整檔接受。
2. `sdk.gen.ts` **匯出函式名集合前後差異**：
   `git diff -- src/client/sdk.gen.ts | grep '^[+-]export const'`。
   新增者逐條對照母 repo `deploy/workflow-editor-gateway/route-inventory/route-classification.yml`
   的 `decision`；指向 **deny** 路由者列於 PR 描述（不移除——閘門仍拒，tree-shaking 不引用即不進 bundle）。
3. 既有匯出簽名若有變動，逐呼叫點修正；`next build` 綠才算完成。
4. 交付態工具型別逐鍵對照：`TransferCallConfig` 應與 `api/schemas/tool.py` 的欄位數一致（W3b 時為 16 鍵）。
