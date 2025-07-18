import { ReactFlowInstance } from '@reactflow/core/dist/esm/types';
import { useContext, useEffect } from 'react';
import { Node, useReactFlow } from 'reactflow';

import {
    LINEAGE_NODE_HEIGHT,
    LINEAGE_NODE_WIDTH,
    LineageNode,
    LineageNodesContext,
    isTransformational,
} from '@app/lineageV3/common';

import { EntityType } from '@types';

export default function useAvoidIntersections(id: string, expandHeight: number, rootType: EntityType, skip = false) {
    const { getNode, getNodes, setNodes } = useReactFlow();

    useEffect(() => {
        if (!skip) {
            avoidIntersections({ id, expandHeight, rootType, getNode, getNodes, setNodes });
        }
    }, [id, expandHeight, rootType, getNode, getNodes, setNodes, skip]);
}

// Required because NodeBuilder cannot properly place Lineage Filter nodes
// TODO: Find a cleaner way to do this
export function useAvoidIntersectionsOften(id: string, expandHeight: number, rootType: EntityType, skip = false) {
    const { getNode, getNodes, setNodes } = useReactFlow();
    const { nodeVersion, displayVersion } = useContext(LineageNodesContext);

    const displayVersionNumber = displayVersion[0];
    useEffect(() => {
        if (!skip) {
            const timeout = setTimeout(
                () => avoidIntersections({ id, expandHeight, rootType, getNode, getNodes, setNodes }),
                0,
            );
            return () => clearTimeout(timeout);
        }
        return () => {};
    }, [id, expandHeight, rootType, getNode, getNodes, setNodes, nodeVersion, displayVersionNumber, skip]);
}

type Arguments = { id: string; expandHeight: number; rootType: EntityType } & Pick<
    ReactFlowInstance<LineageNode>,
    'getNode' | 'getNodes' | 'setNodes'
>;

function avoidIntersections({ id, expandHeight, rootType, getNode, getNodes, setNodes }: Arguments) {
    const self = getNode(id);
    if (!self) {
        return () => {};
    }

    const nodesToMove: Map<string, number> = new Map();
    // Iterate nodes top down
    const nodes = getNodes()
        .filter((node) => !isTransformational(node.data, rootType))
        .filter((node) => node.id !== self.id && node.position.y >= self.position.y && overlapsX(self, node));
    nodes.sort((a, b) => a.position.y - b.position.y);

    let newY = self.position.y + expandHeight;
    // eslint-disable-next-line no-restricted-syntax -- so I can break
    for (const node of nodes) {
        const distance = pushDownDistance(newY, node.position.y);
        if (pushDownDistance(newY, node.position.y)) {
            nodesToMove.set(node.id, distance);
            newY = node.position.y + distance + (node.height || LINEAGE_NODE_HEIGHT);
        } else {
            break;
        }
    }
    if (nodesToMove.size) {
        moveNodes(setNodes, nodesToMove, true);
        return function moveBack() {
            moveNodes(setNodes, nodesToMove, false);
        };
    }
    return () => {};
}

function overlapsX(a: Node, b: Node): boolean {
    return (
        Math.min(a.position.x + (a.width || LINEAGE_NODE_WIDTH), b.position.x + (b.width || LINEAGE_NODE_WIDTH)) >
        Math.max(a.position.x, b.position.x)
    );
}

const MIN_SEPARATION = 10;

function pushDownDistance(newY: number, nodeY: number): number {
    return Math.max(0, newY + MIN_SEPARATION - nodeY);
}

function moveNodes(setNodes: ReactFlowInstance['setNodes'], nodesToMove: Map<string, number>, down: boolean) {
    setNodes((nodes) =>
        nodes.map((node) => {
            const moveAmount = nodesToMove.get(node.id);
            // TODO: Improve interaction with selected nodes? Lacking transition
            if (moveAmount) {
                return {
                    ...node,
                    position: {
                        ...node.position,
                        y: node.position.y + (down ? moveAmount : -moveAmount),
                    },
                };
            }
            return node;
        }),
    );
}
