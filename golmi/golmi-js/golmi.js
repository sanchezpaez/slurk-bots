let golmi_socket = null
let layerView = null
let controller = null

function start_golmi(url, room_id) {
    // expect same as backend e.g. the default "http://127.0.0.1:5000";
    console.log(`Connect to ${url}`)

    // parameters for random initial state
    // (state is generated once the configuration is received)
    const N_OBJECTS = 10;
    const N_GRIPPERS = 1;

    const CUSTOM_CONFIG = {
        "move_step": 0.5,
        "width": 25,
        "height": 25
    };

    // --- create a golmi_socket --- //
    // don't connect yet
    golmi_socket = io(url, {
        auth: { "password": "GiveMeTheBigBluePasswordOnTheLeft" }
    });
    // debug: print any messages to the console
    localStorage.debug = 'golmi_socket.io-client:golmi_socket';

    // --- view --- // 
    // Get references to the three canvas layers
    let bgLayer = document.getElementById("background");
    let objLayer = document.getElementById("objects");
    let grLayer = document.getElementById("gripper");
    
    // set up controller
    controller = new document.LocalKeyController();
    // Set up the view js, this also sets up key listeners
    layerView = new document.LayerView(golmi_socket, bgLayer, objLayer, grLayer);
    grLayer.onclick = (event) => {
        console.log(event.x, event.y)
        console.log(event.target)
        console.log(socket)

        socket.emit("message_command",
            {
                "command": {
                    "target_id": event.target.id,
                    "offset_x": event.offsetX,
                    "offset_y": event.offsetY,
                    "x": event.x,
                    "y": event.y,
                    "block_size": layerView.blockSize,
                },
                "room": self_room
            }
        )
    }

    // --- golmi_socket communication --- //
    golmi_socket.on("connect", () => {
        console.log("Connected to model server");
    });

    golmi_socket.on("disconnect", () => {
        console.log("Disconnected from model server");
    });

    golmi_socket.on("joined_room", (data) => {
        golmi_socket.emit("load_config", CUSTOM_CONFIG);
        console.log(`Joined room ${data.room_id} as client ${data.client_id}`);
    })

    var setup_complete = false;
    golmi_socket.on("update_config", (config) => {
        // only do setup once (reconnections can occur, we don't want to reset the state every time)
        if (!setup_complete && custom_config_is_applied(CUSTOM_CONFIG,
            config)) {
            // ask model to load a random state
            golmi_socket.emit("random_init", {
                "n_objs": N_OBJECTS,
                "n_grippers": N_GRIPPERS,
                "random_gr_position": false,
                "obj_area": "top",
                "target_area": "bottom"
            });
            // manually add a gripper that will be assigned to the controller
            // TODO: Should this happen somewhere else?
            // Options:
            // - automatically get gripper when joining room / use join parameter
            //      -> but then why do we even need pre-generated grippers?
            // - manually add gripper once room is joined
            //      -> but then I need to know generated names? or same problem.
            // so maybe there are 2 approaches that make sense:
            // 1. random init on model side + automatically attach to some gripper that is generated on the fly
            // 2. pass state & attach manually to specific gripper
            golmi_socket.emit("add_gripper");
            setup_complete = true;
        }
    });

    // for debugging: log all events
    golmi_socket.onAny((eventName, ...args) => {
        console.log(eventName, args);
    });

    function custom_config_is_applied(custom_config, config_update) {
        return Object.keys(custom_config).every(key => {
            return config_update[key] == custom_config[key];
        });
    }
}



// --- stop and start drawing --- //
function start() {
    console.log("received url")
    start_golmi(data.command.url, data.command.room_id)
    
    // reset the controller in case any key is currently pressed
    controller.resetKeys()
    controller.attachModel(golmi_socket);
    // manually establish a connection, connect the controller and load a state
    golmi_socket.connect();
    golmi_socket.emit("join", { "room_id": room_id });
}

function stop() {
    // reset the controller in case any key is currently pressed
    controller.resetKeys();
    // disconnect the controller
    controller.detachModel(golmi_socket, "0");
    // manually disconnect
    golmi_socket.disconnect();
}



$(document).ready(function () {
    console.log("starting")
    socket.on("command", (data) => {
        if (typeof (data.command) === "object") {
            // assign role
            if ("role" in data.command) {
                if (data.command.role === "wizard") {
                    set_wizard(data.command.instruction)
                } else if (data.command.role === "player") {
                    set_player(data.command.instruction)
                } else if (data.command.role === "reset") {
                    reset_role(data.command.instruction)
                }

                // board update
            } else if ("url" in data.command) {
                start()
            }
        }
    });
}); // on document ready end
