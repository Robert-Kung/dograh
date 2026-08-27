"use client";

import { useParams, useSearchParams } from "next/navigation";

// customer-center-platform fork（母 repo W2d task 3.4c）：這頁的資料面
// （`GET /workflow/{id}/runs`）對**兩個角色皆 deny** ⇒ 畫面是空的或載入失敗，
// 而空狀態看起來像「還沒有通話」——那是一句假話。
import { useCcpPageNotice } from "@/lib/ccp/notice-bar";

import WorkflowLayout from "../../WorkflowLayout";
import { WorkflowExecutions } from "../components/WorkflowExecutions";

export default function WorkflowRunsPage() {
    const { workflowId } = useParams();
    const searchParams = useSearchParams();
    useCcpPageNotice({
        supervisor: {
            title: '本部署未開放通話紀錄',
            message:
                '通話與測試紀錄的資料面在本部署不經編輯器提供，這個頁面會是空的或載入失敗。'
                + '需要查閱通話紀錄時，請與您的專案窗口提出。',
        },
        implementer: {
            title: '本部署未開放通話紀錄',
            message:
                '`GET /workflow/{id}/runs` 一系列端點在本部署是拒絕的（call-plane）。'
                + '通話紀錄請走部署層的稽核與紀錄面。',
        },
    });

    return (
        <WorkflowLayout showFeaturesNav={false}>
            <WorkflowExecutions
                workflowId={Number(workflowId)}
                searchParams={searchParams}
            />
        </WorkflowLayout>
    );
}
