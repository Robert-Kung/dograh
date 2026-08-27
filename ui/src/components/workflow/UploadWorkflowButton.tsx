'use client';

import { Upload } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useCallback, useState } from 'react';

import { createWorkflowApiV1WorkflowCreateDefinitionPost } from '@/client/sdk.gen';
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAuth } from '@/lib/auth';
// customer-center-platform fork（母 repo W2d task 3.1）：`POST /workflow/create/definition`
// 對主管 403；且 catch 的**頁內**文案原本無條件說「請檢查檔案是否有效」——
// 主管上傳一份完全正確的檔案會被告知一個錯的原因，比沒有原因更糟（gate T-21）。
import { useCcpReadOnly } from '@/lib/ccp/access';
import { ccpErrorText } from '@/lib/ccp/denial';
import { ccpDisabledProps } from '@/lib/ccp/notice-bar';
import logger from '@/lib/logger';
import { getRandomId } from '@/lib/utils';

import { WorkflowData } from '../flow/types';

export function UploadWorkflowButton() {
    const router = useRouter();
    const [isOpen, setIsOpen] = useState(false);
    const [isDragging, setIsDragging] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const { user, getAccessToken } = useAuth();
    const readOnly = useCcpReadOnly();

    const handleFileUpload = useCallback(async (file: File) => {
        try {
            const text = await file.text();
            const workflowData: WorkflowData = JSON.parse(text);

            if (!workflowData.workflow_definition?.nodes ||
                !workflowData.workflow_definition?.edges ||
                !workflowData.workflow_definition?.viewport) {
                throw new Error('Invalid workflow data structure');
            }

            if (!user) return;
            const accessToken = await getAccessToken();
            const response = await createWorkflowApiV1WorkflowCreateDefinitionPost({
                body: {
                    name: workflowData.name || `WF-${getRandomId()}`,
                    workflow_definition: workflowData.workflow_definition as unknown as { [key: string]: unknown },
                },
                headers: {
                    'Authorization': `Bearer ${accessToken}`,
                },
            });

            if (response.error) {
                // 閘門有話說就用閘門的話（拒絕的理由由閘門給，見 task 3.0b）；
                // 只有真的不是拒絕時，才輪得到「檔案可能有問題」這個猜測。
                setError(ccpErrorText(response.error, '上傳失敗，請確認檔案內容是否為有效的工作流 JSON。'));
                return;
            }
            if (response.data?.id) {
                router.push(`/workflow/${response.data.id}`);
                setIsOpen(false);
            }
        } catch (err) {
            setError(ccpErrorText(err, '上傳失敗，請確認檔案內容是否為有效的工作流 JSON。'));
            logger.error(`Error uploading workflow: ${err}`);
        }
    }, [router, user, getAccessToken]);

    const handleDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setIsDragging(false);
        setError(null);

        const file = e.dataTransfer.files[0];
        if (file && file.type === 'application/json') {
            handleFileUpload(file);
        } else {
            setError('Please upload a valid JSON file');
        }
    }, [handleFileUpload]);

    const handleDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setIsDragging(true);
    }, []);

    const handleDragLeave = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setIsDragging(false);
    }, []);

    const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file) {
            handleFileUpload(file);
        }
    }, [handleFileUpload]);

    return (
        <>
            <Button
                onClick={() => setIsOpen(true)}
                variant="outline"
                {...ccpDisabledProps(readOnly)}
            >
                <Upload className="w-4 h-4 mr-2" />
                Upload Agent Definition
            </Button>

            <Dialog open={isOpen} onOpenChange={setIsOpen}>
                <DialogContent className="sm:max-w-md">
                    <DialogHeader>
                        <DialogTitle>Upload Agent Definition</DialogTitle>
                    </DialogHeader>
                    <div
                        className={`mt-4 border-2 border-dashed rounded-lg p-8 text-center ${isDragging ? 'border-primary bg-primary/5' : 'border-gray-300'
                            }`}
                        onDrop={handleDrop}
                        onDragOver={handleDragOver}
                        onDragLeave={handleDragLeave}
                    >
                        <Upload className="w-8 h-8 mx-auto mb-4 text-gray-400" />
                        <p className="text-sm text-gray-600 mb-4">
                            Drag and drop your Workflow JSON File here, or Click to Select
                        </p>
                        <input
                            type="file"
                            accept=".json"
                            onChange={handleFileInput}
                            className="hidden"
                            id="workflow-upload"
                        />
                        <Button
                            variant="outline"
                            onClick={() => document.getElementById('workflow-upload')?.click()}
                        >
                            Select File
                        </Button>
                        {error && (
                            <p className="mt-4 text-sm text-red-600">{error}</p>
                        )}
                    </div>
                </DialogContent>
            </Dialog>
        </>
    );
}
