if ("WebSocket" in window) {
} else {
    alert("data interface not supported, sorry !:(");
}

function getTaggedElement(root, use_id) {
    elementId = `${root}-${use_id}`;
    console.log(`retrieving ${elementId}`);
    return document.getElementById(elementId);
}


function getImageFromFormWebsocket(action, values_from, img_target, use_id) {
    var request = new Object();

    request.action = action;

    function addToRequest(item, index) {
        request[item] = getTaggedElement(item, use_id).value.trim();
    }

    values_from.forEach(addToRequest);

    var websocket_address = "wss://compsyn.fourtheye.xyz/data";
    var websocket = new WebSocket(websocket_address)

    websocket.onopen = function() {
        console.log(`sending webhook from form ${use_id}: ${JSON.stringify(request)}`);
        websocket.send(JSON.stringify(request));
    };
    websocket.onmessage = function(mesg) {
        var data = JSON.parse(mesg.data);
        console.log(`${websocket_address} responded with ${data.status}`)
        var img = getTaggedElement(img_target, use_id);
        switch (data.status) {
            case 200:
                img.src = "data:image/png;base64," + data.found.image_bytes;
                hideForm("selector", use_id)
                break;
            default:
                img.src = "https://www.publicdomainpictures.net/pictures/280000/velka/not-found-image-15383864787lu.jpg";
        }
    };
    websocket.onclose = function() {
        console.log("websocket closing.");
    };
}